#!/usr/bin/env python
# encoding: utf-8
"""
Facilities for launching IPython processes asynchronously.

Authors:

* Brian Granger
* MinRK
"""

#-----------------------------------------------------------------------------
#  Copyright (C) 2008-2011  The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

import copy
import logging
import os
import re
import stat

# signal imports, handling various platforms, versions

from signal import SIGINT, SIGTERM
try:
    from signal import SIGKILL
except ImportError:
    # Windows
    SIGKILL=SIGTERM

try:
    # Windows >= 2.7, 3.2
    from signal import CTRL_C_EVENT as SIGINT
except ImportError:
    pass

from subprocess import Popen, PIPE, STDOUT
try:
    from subprocess import check_output
except ImportError:
    # pre-2.7, define check_output with Popen
    def check_output(*args, **kwargs):
        kwargs.update(dict(stdout=PIPE))
        p = Popen(*args, **kwargs)
        out,err = p.communicate()
        return out

from zmq.eventloop import ioloop

from IPython.config.application import Application
from IPython.config.configurable import LoggingConfigurable
from IPython.utils.text import EvalFormatter
from IPython.utils.traitlets import Any, Int, List, Unicode, Dict, Instance
from IPython.utils.path import get_ipython_module_path
from IPython.utils.process import find_cmd, pycmd2argv, FindCmdError

from .win32support import forward_read_events

from .winhpcjob import IPControllerTask, IPEngineTask, IPControllerJob, IPEngineSetJob

WINDOWS = os.name == 'nt'

#-----------------------------------------------------------------------------
# Paths to the kernel apps
#-----------------------------------------------------------------------------


ipcluster_cmd_argv = pycmd2argv(get_ipython_module_path(
    'IPython.parallel.apps.ipclusterapp'
))

ipengine_cmd_argv = pycmd2argv(get_ipython_module_path(
    'IPython.parallel.apps.ipengineapp'
))

ipcontroller_cmd_argv = pycmd2argv(get_ipython_module_path(
    'IPython.parallel.apps.ipcontrollerapp'
))

#-----------------------------------------------------------------------------
# Base launchers and errors
#-----------------------------------------------------------------------------


class LauncherError(Exception):
    pass


class ProcessStateError(LauncherError):
    pass


class UnknownStatus(LauncherError):
    pass


class BaseLauncher(LoggingConfigurable):
    """An asbtraction for starting, stopping and signaling a process."""

    # In all of the launchers, the work_dir is where child processes will be
    # run. This will usually be the profile_dir, but may not be. any work_dir
    # passed into the __init__ method will override the config value.
    # This should not be used to set the work_dir for the actual engine
    # and controller. Instead, use their own config files or the
    # controller_args, engine_args attributes of the launchers to add
    # the work_dir option.
    work_dir = Unicode('.')
    loop = Instance('zmq.eventloop.ioloop.IOLoop')
    
    start_data = Any()
    stop_data = Any()
    
    def _loop_default(self):
        return ioloop.IOLoop.instance()

    def __init__(self, work_dir='.', config=None, **kwargs):
        super(BaseLauncher, self).__init__(work_dir=work_dir, config=config, **kwargs)
        self.state = 'before' # can be before, running, after
        self.stop_callbacks = []
        self.start_data = None
        self.stop_data = None

    @property
    def args(self):
        """A list of cmd and args that will be used to start the process.

        This is what is passed to :func:`spawnProcess` and the first element
        will be the process name.
        """
        return self.find_args()

    def find_args(self):
        """The ``.args`` property calls this to find the args list.

        Subcommand should implement this to construct the cmd and args.
        """
        raise NotImplementedError('find_args must be implemented in a subclass')

    @property
    def arg_str(self):
        """The string form of the program arguments."""
        return ' '.join(self.args)

    @property
    def running(self):
        """Am I running."""
        if self.state == 'running':
            return True
        else:
            return False

    def start(self):
        """Start the process."""
        raise NotImplementedError('start must be implemented in a subclass')

    def stop(self):
        """Stop the process and notify observers of stopping.

        This method will return None immediately.
        To observe the actual process stopping, see :meth:`on_stop`.
        """
        raise NotImplementedError('stop must be implemented in a subclass')

    def on_stop(self, f):
        """Register a callback to be called with this Launcher's stop_data
        when the process actually finishes.
        """
        if self.state=='after':
            return f(self.stop_data)
        else:
            self.stop_callbacks.append(f)

    def notify_start(self, data):
        """Call this to trigger startup actions.

        This logs the process startup and sets the state to 'running'.  It is
        a pass-through so it can be used as a callback.
        """

        self.log.info('Process %r started: %r' % (self.args[0], data))
        self.start_data = data
        self.state = 'running'
        return data

    def notify_stop(self, data):
        """Call this to trigger process stop actions.

        This logs the process stopping and sets the state to 'after'. Call
        this to trigger callbacks registered via :meth:`on_stop`."""

        self.log.info('Process %r stopped: %r' % (self.args[0], data))
        self.stop_data = data
        self.state = 'after'
        for i in range(len(self.stop_callbacks)):
            d = self.stop_callbacks.pop()
            d(data)
        return data

    def signal(self, sig):
        """Signal the process.

        Parameters
        ----------
        sig : str or int
            'KILL', 'INT', etc., or any signal number
        """
        raise NotImplementedError('signal must be implemented in a subclass')


#-----------------------------------------------------------------------------
# Local process launchers
#-----------------------------------------------------------------------------


class LocalProcessLauncher(BaseLauncher):
    """Start and stop an external process in an asynchronous manner.

    This will launch the external process with a working directory of
    ``self.work_dir``.
    """

    # This is used to to construct self.args, which is passed to 
    # spawnProcess.
    cmd_and_args = List([])
    poll_frequency = Int(100) # in ms

    def __init__(self, work_dir='.', config=None, **kwargs):
        super(LocalProcessLauncher, self).__init__(
            work_dir=work_dir, config=config, **kwargs
        )
        self.process = None
        self.poller = None

    def find_args(self):
        return self.cmd_and_args

    def start(self):
        if self.state == 'before':
            self.process = Popen(self.args,
                stdout=PIPE,stderr=PIPE,stdin=PIPE,
                env=os.environ,
                cwd=self.work_dir
            )
            if WINDOWS:
                self.stdout = forward_read_events(self.process.stdout)
                self.stderr = forward_read_events(self.process.stderr)
            else:
                self.stdout = self.process.stdout.fileno()
                self.stderr = self.process.stderr.fileno()
            self.loop.add_handler(self.stdout, self.handle_stdout, self.loop.READ)
            self.loop.add_handler(self.stderr, self.handle_stderr, self.loop.READ)
            self.poller = ioloop.PeriodicCallback(self.poll, self.poll_frequency, self.loop)
            self.poller.start()
            self.notify_start(self.process.pid)
        else:
            s = 'The process was already started and has state: %r' % self.state
            raise ProcessStateError(s)

    def stop(self):
        return self.interrupt_then_kill()

    def signal(self, sig):
        if self.state == 'running':
            if WINDOWS and sig != SIGINT:
                # use Windows tree-kill for better child cleanup
                check_output(['taskkill', '-pid', str(self.process.pid), '-t', '-f'])
            else:
                self.process.send_signal(sig)

    def interrupt_then_kill(self, delay=2.0):
        """Send INT, wait a delay and then send KILL."""
        try:
            self.signal(SIGINT)
        except Exception:
            self.log.debug("interrupt failed")
            pass
        self.killer  = ioloop.DelayedCallback(lambda : self.signal(SIGKILL), delay*1000, self.loop)
        self.killer.start()

    # callbacks, etc:
    
    def handle_stdout(self, fd, events):
        if WINDOWS:
            line = self.stdout.recv()
        else:
            line = self.process.stdout.readline()
        # a stopped process will be readable but return empty strings
        if line:
            self.log.info(line[:-1])
        else:
            self.poll()
    
    def handle_stderr(self, fd, events):
        if WINDOWS:
            line = self.stderr.recv()
        else:
            line = self.process.stderr.readline()
        # a stopped process will be readable but return empty strings
        if line:
            self.log.error(line[:-1])
        else:
            self.poll()
    
    def poll(self):
        status = self.process.poll()
        if status is not None:
            self.poller.stop()
            self.loop.remove_handler(self.stdout)
            self.loop.remove_handler(self.stderr)
            self.notify_stop(dict(exit_code=status, pid=self.process.pid))
        return status

class LocalControllerLauncher(LocalProcessLauncher):
    """Launch a controller as a regular external process."""

    controller_cmd = List(ipcontroller_cmd_argv, config=True,
        help="""Popen command to launch ipcontroller.""")
    # Command line arguments to ipcontroller.
    controller_args = List(['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="""command-line args to pass to ipcontroller""")

    def find_args(self):
        return self.controller_cmd + self.controller_args

    def start(self, profile_dir):
        """Start the controller by profile_dir."""
        self.controller_args.extend(['profile_dir=%s'%profile_dir])
        self.profile_dir = str(profile_dir)
        self.log.info("Starting LocalControllerLauncher: %r" % self.args)
        return super(LocalControllerLauncher, self).start()


class LocalEngineLauncher(LocalProcessLauncher):
    """Launch a single engine as a regular externall process."""

    engine_cmd = List(ipengine_cmd_argv, config=True,
        help="""command to launch the Engine.""")
    # Command line arguments for ipengine.
    engine_args = List(['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="command-line arguments to pass to ipengine"
    )

    def find_args(self):
        return self.engine_cmd + self.engine_args

    def start(self, profile_dir):
        """Start the engine by profile_dir."""
        self.engine_args.extend(['profile_dir=%s'%profile_dir])
        self.profile_dir = str(profile_dir)
        return super(LocalEngineLauncher, self).start()


class LocalEngineSetLauncher(BaseLauncher):
    """Launch a set of engines as regular external processes."""

    # Command line arguments for ipengine.
    engine_args = List(
        ['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="command-line arguments to pass to ipengine"
    )
    # launcher class
    launcher_class = LocalEngineLauncher
    
    launchers = Dict()
    stop_data = Dict()
    
    def __init__(self, work_dir='.', config=None, **kwargs):
        super(LocalEngineSetLauncher, self).__init__(
            work_dir=work_dir, config=config, **kwargs
        )
        self.stop_data = {}

    def start(self, n, profile_dir):
        """Start n engines by profile or profile_dir."""
        self.profile_dir = str(profile_dir)
        dlist = []
        for i in range(n):
            el = self.launcher_class(work_dir=self.work_dir, config=self.config, log=self.log)
            # Copy the engine args over to each engine launcher.
            el.engine_args = copy.deepcopy(self.engine_args)
            el.on_stop(self._notice_engine_stopped)
            d = el.start(profile_dir)
            if i==0:
                self.log.info("Starting LocalEngineSetLauncher: %r" % el.args)
            self.launchers[i] = el
            dlist.append(d)
        self.notify_start(dlist)
        # The consumeErrors here could be dangerous
        # dfinal = gatherBoth(dlist, consumeErrors=True)
        # dfinal.addCallback(self.notify_start)
        return dlist

    def find_args(self):
        return ['engine set']

    def signal(self, sig):
        dlist = []
        for el in self.launchers.values():
            d = el.signal(sig)
            dlist.append(d)
        # dfinal = gatherBoth(dlist, consumeErrors=True)
        return dlist

    def interrupt_then_kill(self, delay=1.0):
        dlist = []
        for el in self.launchers.values():
            d = el.interrupt_then_kill(delay)
            dlist.append(d)
        # dfinal = gatherBoth(dlist, consumeErrors=True)
        return dlist

    def stop(self):
        return self.interrupt_then_kill()
    
    def _notice_engine_stopped(self, data):
        pid = data['pid']
        for idx,el in self.launchers.items():
            if el.process.pid == pid:
                break
        self.launchers.pop(idx)
        self.stop_data[idx] = data
        if not self.launchers:
            self.notify_stop(self.stop_data)


#-----------------------------------------------------------------------------
# MPIExec launchers
#-----------------------------------------------------------------------------


class MPIExecLauncher(LocalProcessLauncher):
    """Launch an external process using mpiexec."""

    mpi_cmd = List(['mpiexec'], config=True,
        help="The mpiexec command to use in starting the process."
    )
    mpi_args = List([], config=True,
        help="The command line arguments to pass to mpiexec."
    )
    program = List(['date'], config=True,
        help="The program to start via mpiexec.")
    program_args = List([], config=True,
        help="The command line argument to the program."
    )
    n = Int(1)

    def find_args(self):
        """Build self.args using all the fields."""
        return self.mpi_cmd + ['-n', str(self.n)] + self.mpi_args + \
               self.program + self.program_args

    def start(self, n):
        """Start n instances of the program using mpiexec."""
        self.n = n
        return super(MPIExecLauncher, self).start()


class MPIExecControllerLauncher(MPIExecLauncher):
    """Launch a controller using mpiexec."""

    controller_cmd = List(ipcontroller_cmd_argv, config=True,
        help="Popen command to launch the Contropper"
    )
    controller_args = List(['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="Command line arguments to pass to ipcontroller."
    )
    n = Int(1)

    def start(self, profile_dir):
        """Start the controller by profile_dir."""
        self.controller_args.extend(['profile_dir=%s'%profile_dir])
        self.profile_dir = str(profile_dir)
        self.log.info("Starting MPIExecControllerLauncher: %r" % self.args)
        return super(MPIExecControllerLauncher, self).start(1)

    def find_args(self):
        return self.mpi_cmd + ['-n', self.n] + self.mpi_args + \
               self.controller_cmd + self.controller_args


class MPIExecEngineSetLauncher(MPIExecLauncher):

    program = List(ipengine_cmd_argv, config=True,
        help="Popen command for ipengine"
    )
    program_args = List(
        ['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="Command line arguments for ipengine."
    )
    n = Int(1)

    def start(self, n, profile_dir):
        """Start n engines by profile or profile_dir."""
        self.program_args.extend(['profile_dir=%s'%profile_dir])
        self.profile_dir = str(profile_dir)
        self.n = n
        self.log.info('Starting MPIExecEngineSetLauncher: %r' % self.args)
        return super(MPIExecEngineSetLauncher, self).start(n)

#-----------------------------------------------------------------------------
# SSH launchers
#-----------------------------------------------------------------------------

# TODO: Get SSH Launcher back to level of sshx in 0.10.2

class SSHLauncher(LocalProcessLauncher):
    """A minimal launcher for ssh.

    To be useful this will probably have to be extended to use the ``sshx``
    idea for environment variables.  There could be other things this needs
    as well.
    """

    ssh_cmd = List(['ssh'], config=True,
        help="command for starting ssh")
    ssh_args = List(['-tt'], config=True,
        help="args to pass to ssh")
    program = List(['date'], config=True,
        help="Program to launch via ssh")
    program_args = List([], config=True,
        help="args to pass to remote program")
    hostname = Unicode('', config=True,
        help="hostname on which to launch the program")
    user = Unicode('', config=True,
        help="username for ssh")
    location = Unicode('', config=True,
        help="user@hostname location for ssh in one setting")

    def _hostname_changed(self, name, old, new):
        if self.user:
            self.location = '%s@%s' % (self.user, new)
        else:
            self.location = new

    def _user_changed(self, name, old, new):
        self.location = '%s@%s' % (new, self.hostname)

    def find_args(self):
        return self.ssh_cmd + self.ssh_args + [self.location] + \
               self.program + self.program_args

    def start(self, profile_dir, hostname=None, user=None):
        self.profile_dir = str(profile_dir)
        if hostname is not None:
            self.hostname = hostname
        if user is not None:
            self.user = user
        
        return super(SSHLauncher, self).start()
    
    def signal(self, sig):
        if self.state == 'running':
            # send escaped ssh connection-closer
            self.process.stdin.write('~.')
            self.process.stdin.flush()
        


class SSHControllerLauncher(SSHLauncher):

    program = List(ipcontroller_cmd_argv, config=True,
        help="remote ipcontroller command.")
    program_args = List(['--reuse-files', '--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="Command line arguments to ipcontroller.")


class SSHEngineLauncher(SSHLauncher):
    program = List(ipengine_cmd_argv, config=True,
        help="remote ipengine command.")
    # Command line arguments for ipengine.
    program_args = List(
        ['--log-to-file','log_level=%i'%logging.INFO], config=True,
        help="Command line arguments to ipengine."
    )
    
class SSHEngineSetLauncher(LocalEngineSetLauncher):
    launcher_class = SSHEngineLauncher
    engines = Dict(config=True,
        help="""dict of engines to launch.  This is a dict by hostname of ints,
        corresponding to the number of engines to start on that host.""")
    
    def start(self, n, profile_dir):
        """Start engines by profile or profile_dir.
        `n` is ignored, and the `engines` config property is used instead.
        """
        
        self.profile_dir = str(profile_dir)
        dlist = []
        for host, n in self.engines.items():
            if isinstance(n, (tuple, list)):
                n, args = n
            else:
                args = copy.deepcopy(self.engine_args)
            
            if '@' in host:
                user,host = host.split('@',1)
            else:
                user=None
            for i in range(n):
                el = self.launcher_class(work_dir=self.work_dir, config=self.config, log=self.log)
                
                # Copy the engine args over to each engine launcher.
                i
                el.program_args = args
                el.on_stop(self._notice_engine_stopped)
                d = el.start(profile_dir, user=user, hostname=host)
                if i==0:
                    self.log.info("Starting SSHEngineSetLauncher: %r" % el.args)
                self.launchers[host+str(i)] = el
                dlist.append(d)
        self.notify_start(dlist)
        return dlist
    


#-----------------------------------------------------------------------------
# Windows HPC Server 2008 scheduler launchers
#-----------------------------------------------------------------------------


# This is only used on Windows.
def find_job_cmd():
    if WINDOWS:
        try:
            return find_cmd('job')
        except (FindCmdError, ImportError):
            # ImportError will be raised if win32api is not installed
            return 'job'
    else:
        return 'job'


class WindowsHPCLauncher(BaseLauncher):

    job_id_regexp = Unicode(r'\d+', config=True,
        help="""A regular expression used to get the job id from the output of the
        submit_command. """
        )
    job_file_name = Unicode('ipython_job.xml', config=True,
        help="The filename of the instantiated job script.")
    # The full path to the instantiated job script. This gets made dynamically
    # by combining the work_dir with the job_file_name.
    job_file = Unicode('')
    scheduler = Unicode('', config=True,
        help="The hostname of the scheduler to submit the job to.")
    job_cmd = Unicode(find_job_cmd(), config=True,
        help="The command for submitting jobs.")

    def __init__(self, work_dir='.', config=None, **kwargs):
        super(WindowsHPCLauncher, self).__init__(
            work_dir=work_dir, config=config, **kwargs
        )

    @property
    def job_file(self):
        return os.path.join(self.work_dir, self.job_file_name)

    def write_job_file(self, n):
        raise NotImplementedError("Implement write_job_file in a subclass.")

    def find_args(self):
        return ['job.exe']
        
    def parse_job_id(self, output):
        """Take the output of the submit command and return the job id."""
        m = re.search(self.job_id_regexp, output)
        if m is not None:
            job_id = m.group()
        else:
            raise LauncherError("Job id couldn't be determined: %s" % output)
        self.job_id = job_id
        self.log.info('Job started with job id: %r' % job_id)
        return job_id

    def start(self, n):
        """Start n copies of the process using the Win HPC job scheduler."""
        self.write_job_file(n)
        args = [
            'submit',
            '/jobfile:%s' % self.job_file,
            '/scheduler:%s' % self.scheduler
        ]
        self.log.info("Starting Win HPC Job: %s" % (self.job_cmd + ' ' + ' '.join(args),))

        output = check_output([self.job_cmd]+args,
            env=os.environ,
            cwd=self.work_dir,
            stderr=STDOUT
        )
        job_id = self.parse_job_id(output)
        self.notify_start(job_id)
        return job_id

    def stop(self):
        args = [
            'cancel',
            self.job_id,
            '/scheduler:%s' % self.scheduler
        ]
        self.log.info("Stopping Win HPC Job: %s" % (self.job_cmd + ' ' + ' '.join(args),))
        try:
            output = check_output([self.job_cmd]+args,
                env=os.environ,
                cwd=self.work_dir,
                stderr=STDOUT
            )
        except:
            output = 'The job already appears to be stoppped: %r' % self.job_id
        self.notify_stop(dict(job_id=self.job_id, output=output))  # Pass the output of the kill cmd
        return output


class WindowsHPCControllerLauncher(WindowsHPCLauncher):

    job_file_name = Unicode('ipcontroller_job.xml', config=True,
        help="WinHPC xml job file.")
    extra_args = List([], config=False,
        help="extra args to pass to ipcontroller")

    def write_job_file(self, n):
        job = IPControllerJob(config=self.config)

        t = IPControllerTask(config=self.config)
        # The tasks work directory is *not* the actual work directory of 
        # the controller. It is used as the base path for the stdout/stderr
        # files that the scheduler redirects to.
        t.work_directory = self.profile_dir
        # Add the profile_dir and from self.start().
        t.controller_args.extend(self.extra_args)
        job.add_task(t)

        self.log.info("Writing job description file: %s" % self.job_file)
        job.write(self.job_file)

    @property
    def job_file(self):
        return os.path.join(self.profile_dir, self.job_file_name)

    def start(self, profile_dir):
        """Start the controller by profile_dir."""
        self.extra_args = ['profile_dir=%s'%profile_dir]
        self.profile_dir = str(profile_dir)
        return super(WindowsHPCControllerLauncher, self).start(1)


class WindowsHPCEngineSetLauncher(WindowsHPCLauncher):

    job_file_name = Unicode('ipengineset_job.xml', config=True,
        help="jobfile for ipengines job")
    extra_args = List([], config=False,
        help="extra args to pas to ipengine")

    def write_job_file(self, n):
        job = IPEngineSetJob(config=self.config)

        for i in range(n):
            t = IPEngineTask(config=self.config)
            # The tasks work directory is *not* the actual work directory of 
            # the engine. It is used as the base path for the stdout/stderr
            # files that the scheduler redirects to.
            t.work_directory = self.profile_dir
            # Add the profile_dir and from self.start().
            t.engine_args.extend(self.extra_args)
            job.add_task(t)

        self.log.info("Writing job description file: %s" % self.job_file)
        job.write(self.job_file)

    @property
    def job_file(self):
        return os.path.join(self.profile_dir, self.job_file_name)

    def start(self, n, profile_dir):
        """Start the controller by profile_dir."""
        self.extra_args = ['profile_dir=%s'%profile_dir]
        self.profile_dir = str(profile_dir)
        return super(WindowsHPCEngineSetLauncher, self).start(n)


#-----------------------------------------------------------------------------
# Batch (PBS) system launchers
#-----------------------------------------------------------------------------

class BatchSystemLauncher(BaseLauncher):
    """Launch an external process using a batch system.

    This class is designed to work with UNIX batch systems like PBS, LSF,
    GridEngine, etc.  The overall model is that there are different commands
    like qsub, qdel, etc. that handle the starting and stopping of the process.

    This class also has the notion of a batch script. The ``batch_template``
    attribute can be set to a string that is a template for the batch script.
    This template is instantiated using string formatting. Thus the template can
    use {n} fot the number of instances. Subclasses can add additional variables
    to the template dict.
    """

    # Subclasses must fill these in.  See PBSEngineSet
    submit_command = List([''], config=True,
        help="The name of the command line program used to submit jobs.")
    delete_command = List([''], config=True,
        help="The name of the command line program used to delete jobs.")
    job_id_regexp = Unicode('', config=True,
        help="""A regular expression used to get the job id from the output of the
        submit_command.""")
    batch_template = Unicode('', config=True,
        help="The string that is the batch script template itself.")
    batch_template_file = Unicode('', config=True,
        help="The file that contains the batch template.")
    batch_file_name = Unicode('batch_script', config=True,
        help="The filename of the instantiated batch script.")
    queue = Unicode('', config=True,
        help="The PBS Queue.")
    
    # not configurable, override in subclasses
    # PBS Job Array regex
    job_array_regexp = Unicode('')
    job_array_template = Unicode('')
    # PBS Queue regex
    queue_regexp = Unicode('')
    queue_template = Unicode('')
    # The default batch template, override in subclasses
    default_template = Unicode('')
    # The full path to the instantiated batch script.
    batch_file = Unicode('')
    # the format dict used with batch_template:
    context = Dict()
    # the Formatter instance for rendering the templates:
    formatter = Instance(EvalFormatter, (), {})

    
    def find_args(self):
        return self.submit_command + [self.batch_file]
    
    def __init__(self, work_dir='.', config=None, **kwargs):
        super(BatchSystemLauncher, self).__init__(
            work_dir=work_dir, config=config, **kwargs
        )
        self.batch_file = os.path.join(self.work_dir, self.batch_file_name)

    def parse_job_id(self, output):
        """Take the output of the submit command and return the job id."""
        m = re.search(self.job_id_regexp, output)
        if m is not None:
            job_id = m.group()
        else:
            raise LauncherError("Job id couldn't be determined: %s" % output)
        self.job_id = job_id
        self.log.info('Job submitted with job id: %r' % job_id)
        return job_id

    def write_batch_script(self, n):
        """Instantiate and write the batch script to the work_dir."""
        self.context['n'] = n
        self.context['queue'] = self.queue
        # first priority is batch_template if set
        if self.batch_template_file and not self.batch_template:
            # second priority is batch_template_file
            with open(self.batch_template_file) as f:
                self.batch_template = f.read()
        if not self.batch_template:
            # third (last) priority is default_template
            self.batch_template = self.default_template
        
        regex = re.compile(self.job_array_regexp)
        # print regex.search(self.batch_template)
        if not regex.search(self.batch_template):
            self.log.info("adding job array settings to batch script")
            firstline, rest = self.batch_template.split('\n',1)
            self.batch_template = '\n'.join([firstline, self.job_array_template, rest])
        
        regex = re.compile(self.queue_regexp)
        # print regex.search(self.batch_template)
        if self.queue and not regex.search(self.batch_template):
            self.log.info("adding PBS queue settings to batch script")
            firstline, rest = self.batch_template.split('\n',1)
            self.batch_template = '\n'.join([firstline, self.queue_template, rest])
            
        script_as_string = self.formatter.format(self.batch_template, **self.context)
        self.log.info('Writing instantiated batch script: %s' % self.batch_file)
    
        with open(self.batch_file, 'w') as f:
            f.write(script_as_string)
        os.chmod(self.batch_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def start(self, n, profile_dir):
        """Start n copies of the process using a batch system."""
        # Here we save profile_dir in the context so they
        # can be used in the batch script template as {profile_dir}
        self.context['profile_dir'] = profile_dir
        self.profile_dir = str(profile_dir)
        self.write_batch_script(n)
        output = check_output(self.args, env=os.environ)
        
        job_id = self.parse_job_id(output)
        self.notify_start(job_id)
        return job_id

    def stop(self):
        output = check_output(self.delete_command+[self.job_id], env=os.environ)
        self.notify_stop(dict(job_id=self.job_id, output=output)) # Pass the output of the kill cmd
        return output


class PBSLauncher(BatchSystemLauncher):
    """A BatchSystemLauncher subclass for PBS."""

    submit_command = List(['qsub'], config=True,
        help="The PBS submit command ['qsub']")
    delete_command = List(['qdel'], config=True,
        help="The PBS delete command ['qsub']")
    job_id_regexp = Unicode(r'\d+', config=True,
        help="Regular expresion for identifying the job ID [r'\d+']")
    
    batch_file = Unicode('')
    job_array_regexp = Unicode('#PBS\W+-t\W+[\w\d\-\$]+')
    job_array_template = Unicode('#PBS -t 1-{n}')
    queue_regexp = Unicode('#PBS\W+-q\W+\$?\w+')
    queue_template = Unicode('#PBS -q {queue}')


class PBSControllerLauncher(PBSLauncher):
    """Launch a controller using PBS."""

    batch_file_name = Unicode('pbs_controller', config=True,
        help="batch file name for the controller job.")
    default_template= Unicode("""#!/bin/sh
#PBS -V
#PBS -N ipcontroller
%s --log-to-file profile_dir={profile_dir}
"""%(' '.join(ipcontroller_cmd_argv)))

    def start(self, profile_dir):
        """Start the controller by profile or profile_dir."""
        self.log.info("Starting PBSControllerLauncher: %r" % self.args)
        return super(PBSControllerLauncher, self).start(1, profile_dir)


class PBSEngineSetLauncher(PBSLauncher):
    """Launch Engines using PBS"""
    batch_file_name = Unicode('pbs_engines', config=True,
        help="batch file name for the engine(s) job.")
    default_template= Unicode("""#!/bin/sh
#PBS -V
#PBS -N ipengine
%s profile_dir={profile_dir}
"""%(' '.join(ipengine_cmd_argv)))

    def start(self, n, profile_dir):
        """Start n engines by profile or profile_dir."""
        self.log.info('Starting %i engines with PBSEngineSetLauncher: %r' % (n, self.args))
        return super(PBSEngineSetLauncher, self).start(n, profile_dir)

#SGE is very similar to PBS

class SGELauncher(PBSLauncher):
    """Sun GridEngine is a PBS clone with slightly different syntax"""
    job_array_regexp = Unicode('#\$\W+\-t')
    job_array_template = Unicode('#$ -t 1-{n}')
    queue_regexp = Unicode('#\$\W+-q\W+\$?\w+')
    queue_template = Unicode('#$ -q {queue}')

class SGEControllerLauncher(SGELauncher):
    """Launch a controller using SGE."""

    batch_file_name = Unicode('sge_controller', config=True,
        help="batch file name for the ipontroller job.")
    default_template= Unicode("""#$ -V
#$ -S /bin/sh
#$ -N ipcontroller
%s --log-to-file profile_dir={profile_dir}
"""%(' '.join(ipcontroller_cmd_argv)))

    def start(self, profile_dir):
        """Start the controller by profile or profile_dir."""
        self.log.info("Starting PBSControllerLauncher: %r" % self.args)
        return super(SGEControllerLauncher, self).start(1, profile_dir)

class SGEEngineSetLauncher(SGELauncher):
    """Launch Engines with SGE"""
    batch_file_name = Unicode('sge_engines', config=True,
        help="batch file name for the engine(s) job.")
    default_template = Unicode("""#$ -V
#$ -S /bin/sh
#$ -N ipengine
%s profile_dir={profile_dir}
"""%(' '.join(ipengine_cmd_argv)))

    def start(self, n, profile_dir):
        """Start n engines by profile or profile_dir."""
        self.log.info('Starting %i engines with SGEEngineSetLauncher: %r' % (n, self.args))
        return super(SGEEngineSetLauncher, self).start(n, profile_dir)


#-----------------------------------------------------------------------------
# A launcher for ipcluster itself!
#-----------------------------------------------------------------------------


class IPClusterLauncher(LocalProcessLauncher):
    """Launch the ipcluster program in an external process."""

    ipcluster_cmd = List(ipcluster_cmd_argv, config=True,
        help="Popen command for ipcluster")
    ipcluster_args = List(
        ['--clean-logs', '--log-to-file', 'log_level=%i'%logging.INFO], config=True,
        help="Command line arguments to pass to ipcluster.")
    ipcluster_subcommand = Unicode('start')
    ipcluster_n = Int(2)

    def find_args(self):
        return self.ipcluster_cmd + ['--'+self.ipcluster_subcommand] + \
            ['n=%i'%self.ipcluster_n] + self.ipcluster_args

    def start(self):
        self.log.info("Starting ipcluster: %r" % self.args)
        return super(IPClusterLauncher, self).start()

#-----------------------------------------------------------------------------
# Collections of launchers
#-----------------------------------------------------------------------------

local_launchers = [
    LocalControllerLauncher,
    LocalEngineLauncher,
    LocalEngineSetLauncher,
]
mpi_launchers = [
    MPIExecLauncher,
    MPIExecControllerLauncher,
    MPIExecEngineSetLauncher,
]
ssh_launchers = [
    SSHLauncher,
    SSHControllerLauncher,
    SSHEngineLauncher,
    SSHEngineSetLauncher,
]
winhpc_launchers = [
    WindowsHPCLauncher,
    WindowsHPCControllerLauncher,
    WindowsHPCEngineSetLauncher,
]
pbs_launchers = [
    PBSLauncher,
    PBSControllerLauncher,
    PBSEngineSetLauncher,
]
sge_launchers = [
    SGELauncher,
    SGEControllerLauncher,
    SGEEngineSetLauncher,
]
all_launchers = local_launchers + mpi_launchers + ssh_launchers + winhpc_launchers\
                + pbs_launchers + sge_launchers

