"""Implement a fully blocking kernel manager.

Useful for test suites and blocking terminal interfaces.
"""
#-----------------------------------------------------------------------------
#  Copyright (C) 2010  The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING.txt, distributed as part of this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------


# Stdlib
from queue import Queue, Empty

# Our own
from IPython.utils import io
from IPython.utils.traitlets import Type

from .kernelmanager import (KernelManager, SubSocketChannel, HBSocketChannel,
                           ShellSocketChannel, StdInSocketChannel)

#-----------------------------------------------------------------------------
# Functions and classes
#-----------------------------------------------------------------------------

class BlockingSubSocketChannel(SubSocketChannel):

    def __init__(self, context, session, address=None):
        super(BlockingSubSocketChannel, self).__init__(context, session,
                                                       address)
        self._in_queue = Queue()

    def call_handlers(self, msg):
        #io.rprint('[[Sub]]', msg) # dbg
        self._in_queue.put(msg)

    def msg_ready(self):
        """Is there a message that has been received?"""
        if self._in_queue.qsize() == 0:
            return False
        else:
            return True

    def get_msg(self, block=True, timeout=None):
        """Get a message if there is one that is ready."""
        return self._in_queue.get(block, timeout)

    def get_msgs(self):
        """Get all messages that are currently ready."""
        msgs = []
        while True:
            try:
                msgs.append(self.get_msg(block=False))
            except Empty:
                break
        return msgs


class BlockingShellSocketChannel(ShellSocketChannel):

    def __init__(self, context, session, address=None):
        super(BlockingShellSocketChannel, self).__init__(context, session,
                                                        address)
        self._in_queue = Queue()

    def call_handlers(self, msg):
        #io.rprint('[[Shell]]', msg) # dbg
        self._in_queue.put(msg)

    def msg_ready(self):
        """Is there a message that has been received?"""
        if self._in_queue.qsize() == 0:
            return False
        else:
            return True

    def get_msg(self, block=True, timeout=None):
        """Get a message if there is one that is ready."""
        return self._in_queue.get(block, timeout)

    def get_msgs(self):
        """Get all messages that are currently ready."""
        msgs = []
        while True:
            try:
                msgs.append(self.get_msg(block=False))
            except Empty:
                break
        return msgs
    

class BlockingStdInSocketChannel(StdInSocketChannel):
    
    def call_handlers(self, msg):
        #io.rprint('[[Rep]]', msg) # dbg
        pass


class BlockingHBSocketChannel(HBSocketChannel):
    
    # This kernel needs rapid monitoring capabilities
    time_to_dead = 0.2

    def call_handlers(self, since_last_heartbeat):
        #io.rprint('[[Heart]]', since_last_heartbeat) # dbg
        pass


class BlockingKernelManager(KernelManager):
    
    # The classes to use for the various channels.
    shell_channel_class = Type(BlockingShellSocketChannel)
    sub_channel_class = Type(BlockingSubSocketChannel)
    stdin_channel_class = Type(BlockingStdInSocketChannel)
    hb_channel_class = Type(BlockingHBSocketChannel)
  
