#!/usr/bin/env python
"""
Kernel adapted from kernel.py to use ZMQ Streams
"""
#-----------------------------------------------------------------------------
#  Copyright (C) 2010-2011  The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

# Standard library imports.


import sys
import time

from code import CommandCompiler
from datetime import datetime
from pprint import pprint

# System library imports.
import zmq
from zmq.eventloop import ioloop, zmqstream

# Local imports.
from IPython.utils.traitlets import Instance, List, Int, Dict, Set, Str
from IPython.zmq.completer import KernelCompleter

from IPython.parallel.error import wrap_exception
from IPython.parallel.factory import SessionFactory
from IPython.parallel.util import serialize_object, unpack_apply_message, ISO8601

def printer(*args):
    pprint(args, stream=sys.__stdout__)


class _Passer(zmqstream.ZMQStream):
    """Empty class that implements `send()` that does nothing.
    
    Subclass ZMQStream for StreamSession typechecking
    
    """
    def __init__(self, *args, **kwargs):
        pass
    
    def send(self, *args, **kwargs):
        pass
    send_multipart = send
    

#-----------------------------------------------------------------------------
# Main kernel class
#-----------------------------------------------------------------------------

class Kernel(SessionFactory):

    #---------------------------------------------------------------------------
    # Kernel interface
    #---------------------------------------------------------------------------
    
    # kwargs:
    int_id = Int(-1, config=True)
    user_ns = Dict(config=True)
    exec_lines = List(config=True)
    
    control_stream = Instance(zmqstream.ZMQStream)
    task_stream = Instance(zmqstream.ZMQStream)
    iopub_stream = Instance(zmqstream.ZMQStream)
    client = Instance('IPython.parallel.Client')
    
    # internals
    shell_streams = List()
    compiler = Instance(CommandCompiler, (), {})
    completer = Instance(KernelCompleter)
    
    aborted = Set()
    shell_handlers = Dict()
    control_handlers = Dict()
    
    def _set_prefix(self):
        self.prefix = "engine.%s"%self.int_id
    
    def _connect_completer(self):
        self.completer = KernelCompleter(self.user_ns)
    
    def __init__(self, **kwargs):
        super(Kernel, self).__init__(**kwargs)
        self._set_prefix()
        self._connect_completer()
        
        self.on_trait_change(self._set_prefix, 'id')
        self.on_trait_change(self._connect_completer, 'user_ns')
        
        # Build dict of handlers for message types
        for msg_type in ['execute_request', 'complete_request', 'apply_request', 
                'clear_request']:
            self.shell_handlers[msg_type] = getattr(self, msg_type)
        
        for msg_type in ['shutdown_request', 'abort_request']+list(self.shell_handlers.keys()):
            self.control_handlers[msg_type] = getattr(self, msg_type)
        
        self._initial_exec_lines()
    
    def _wrap_exception(self, method=None):
        e_info = dict(engine_uuid=self.ident, engine_id=self.int_id, method=method)
        content=wrap_exception(e_info)
        return content
    
    def _initial_exec_lines(self):
        s = _Passer()
        content = dict(silent=True, user_variable=[],user_expressions=[])
        for line in self.exec_lines:
            self.log.debug("executing initialization: %s"%line)
            content.update({'code':line})
            msg = self.session.msg('execute_request', content)
            self.execute_request(s, [], msg)
        
        
    #-------------------- control handlers -----------------------------
    def abort_queues(self):
        for stream in self.shell_streams:
            if stream:
                self.abort_queue(stream)
    
    def abort_queue(self, stream):
        while True:
            try:
                msg = self.session.recv(stream, zmq.NOBLOCK,content=True)
            except zmq.ZMQError as e:
                if e.errno == zmq.EAGAIN:
                    break
                else:
                    return
            else:
                if msg is None:
                    return
                else:
                    idents,msg = msg
                
                # assert self.reply_socketly_socket.rcvmore(), "Unexpected missing message part."
                # msg = self.reply_socket.recv_json()
            self.log.info("Aborting:")
            self.log.info(str(msg))
            msg_type = msg['msg_type']
            reply_type = msg_type.split('_')[0] + '_reply'
            # reply_msg = self.session.msg(reply_type, {'status' : 'aborted'}, msg)
            # self.reply_socket.send(ident,zmq.SNDMORE)
            # self.reply_socket.send_json(reply_msg)
            reply_msg = self.session.send(stream, reply_type, 
                        content={'status' : 'aborted'}, parent=msg, ident=idents)[0]
            self.log.debug(str(reply_msg))
            # We need to wait a bit for requests to come in. This can probably
            # be set shorter for true asynchronous clients.
            time.sleep(0.05)
    
    def abort_request(self, stream, ident, parent):
        """abort a specifig msg by id"""
        msg_ids = parent['content'].get('msg_ids', None)
        if isinstance(msg_ids, str):
            msg_ids = [msg_ids]
        if not msg_ids:
            self.abort_queues()
        for mid in msg_ids:
            self.aborted.add(str(mid))
        
        content = dict(status='ok')
        reply_msg = self.session.send(stream, 'abort_reply', content=content, 
                parent=parent, ident=ident)
        self.log.debug(str(reply_msg))
    
    def shutdown_request(self, stream, ident, parent):
        """kill ourself.  This should really be handled in an external process"""
        try:
            self.abort_queues()
        except:
            content = self._wrap_exception('shutdown')
        else:
            content = dict(parent['content'])
            content['status'] = 'ok'
        msg = self.session.send(stream, 'shutdown_reply',
                                content=content, parent=parent, ident=ident)
        self.log.debug(str(msg))
        dc = ioloop.DelayedCallback(lambda : sys.exit(0), 1000, self.loop)
        dc.start()
    
    def dispatch_control(self, msg):
        idents,msg = self.session.feed_identities(msg, copy=False)
        try:
            msg = self.session.unpack_message(msg, content=True, copy=False)
        except:
            self.log.error("Invalid Message", exc_info=True)
            return
        
        header = msg['header']
        msg_id = header['msg_id']
        
        handler = self.control_handlers.get(msg['msg_type'], None)
        if handler is None:
            self.log.error("UNKNOWN CONTROL MESSAGE TYPE: %r"%msg['msg_type'])
        else:
            handler(self.control_stream, idents, msg)
    

    #-------------------- queue helpers ------------------------------
    
    def check_dependencies(self, dependencies):
        if not dependencies:
            return True
        if len(dependencies) == 2 and dependencies[0] in 'any all'.split():
            anyorall = dependencies[0]
            dependencies = dependencies[1]
        else:
            anyorall = 'all'
        results = self.client.get_results(dependencies,status_only=True)
        if results['status'] != 'ok':
            return False
        
        if anyorall == 'any':
            if not results['completed']:
                return False
        else:
            if results['pending']:
                return False
        
        return True
    
    def check_aborted(self, msg_id):
        return msg_id in self.aborted
    
    #-------------------- queue handlers -----------------------------
    
    def clear_request(self, stream, idents, parent):
        """Clear our namespace."""
        self.user_ns = {}
        msg = self.session.send(stream, 'clear_reply', ident=idents, parent=parent, 
                content = dict(status='ok'))
        self._initial_exec_lines()
    
    def execute_request(self, stream, ident, parent):
        self.log.debug('execute request %s'%parent)
        try:
            code = parent['content']['code']
        except:
            self.log.error("Got bad msg: %s"%parent, exc_info=True)
            return
        self.session.send(self.iopub_stream, 'pyin', {'code':code},parent=parent,
                            ident='%s.pyin'%self.prefix)
        started = datetime.now().strftime(ISO8601)
        try:
            comp_code = self.compiler(code, '<zmq-kernel>')
            # allow for not overriding displayhook
            if hasattr(sys.displayhook, 'set_parent'):
                sys.displayhook.set_parent(parent)
                sys.stdout.set_parent(parent)
                sys.stderr.set_parent(parent)
            exec(comp_code, self.user_ns, self.user_ns)
        except:
            exc_content = self._wrap_exception('execute')
            # exc_msg = self.session.msg(u'pyerr', exc_content, parent)
            self.session.send(self.iopub_stream, 'pyerr', exc_content, parent=parent,
                            ident='%s.pyerr'%self.prefix)
            reply_content = exc_content
        else:
            reply_content = {'status' : 'ok'}
        
        reply_msg = self.session.send(stream, 'execute_reply', reply_content, parent=parent, 
                    ident=ident, subheader = dict(started=started))
        self.log.debug(str(reply_msg))
        if reply_msg['content']['status'] == 'error':
            self.abort_queues()

    def complete_request(self, stream, ident, parent):
        matches = {'matches' : self.complete(parent),
                   'status' : 'ok'}
        completion_msg = self.session.send(stream, 'complete_reply',
                                           matches, parent, ident)
        # print >> sys.__stdout__, completion_msg

    def complete(self, msg):
        return self.completer.complete(msg.content.line, msg.content.text)
    
    def apply_request(self, stream, ident, parent):
        # flush previous reply, so this request won't block it
        stream.flush(zmq.POLLOUT)
        
        try:
            content = parent['content']
            bufs = parent['buffers']
            msg_id = parent['header']['msg_id']
            # bound = parent['header'].get('bound', False)
        except:
            self.log.error("Got bad msg: %s"%parent, exc_info=True)
            return
        # pyin_msg = self.session.msg(u'pyin',{u'code':code}, parent=parent)
        # self.iopub_stream.send(pyin_msg)
        # self.session.send(self.iopub_stream, u'pyin', {u'code':code},parent=parent)
        sub = {'dependencies_met' : True, 'engine' : self.ident,
                'started': datetime.now().strftime(ISO8601)}
        try:
            # allow for not overriding displayhook
            if hasattr(sys.displayhook, 'set_parent'):
                sys.displayhook.set_parent(parent)
                sys.stdout.set_parent(parent)
                sys.stderr.set_parent(parent)
            # exec "f(*args,**kwargs)" in self.user_ns, self.user_ns
            working = self.user_ns
            # suffix = 
            prefix = "_"+str(msg_id).replace("-","")+"_"
            
            f,args,kwargs = unpack_apply_message(bufs, working, copy=False)
            # if bound:
            #     bound_ns = Namespace(working)
            #     args = [bound_ns]+list(args)

            fname = getattr(f, '__name__', 'f')
            
            fname = prefix+"f"
            argname = prefix+"args"
            kwargname = prefix+"kwargs"
            resultname = prefix+"result"
            
            ns = { fname : f, argname : args, kwargname : kwargs , resultname : None }
            # print ns
            working.update(ns)
            code = "%s=%s(*%s,**%s)"%(resultname, fname, argname, kwargname)
            try:
                exec(code, working,working)
                result = working.get(resultname)
            finally:
                for key in ns.keys():
                    working.pop(key)
            # if bound:
            #     working.update(bound_ns)
            
            packed_result,buf = serialize_object(result)
            result_buf = [packed_result]+buf
        except:
            exc_content = self._wrap_exception('apply')
            # exc_msg = self.session.msg(u'pyerr', exc_content, parent)
            self.session.send(self.iopub_stream, 'pyerr', exc_content, parent=parent,
                                ident='%s.pyerr'%self.prefix)
            reply_content = exc_content
            result_buf = []
            
            if exc_content['ename'] == 'UnmetDependency':
                sub['dependencies_met'] = False
        else:
            reply_content = {'status' : 'ok'}
        
        # put 'ok'/'error' status in header, for scheduler introspection:
        sub['status'] = reply_content['status']
        
        reply_msg = self.session.send(stream, 'apply_reply', reply_content, 
                    parent=parent, ident=ident,buffers=result_buf, subheader=sub)
        
        # flush i/o
        # should this be before reply_msg is sent, like in the single-kernel code, 
        # or should nothing get in the way of real results?
        sys.stdout.flush()
        sys.stderr.flush()
    
    def dispatch_queue(self, stream, msg):
        self.control_stream.flush()
        idents,msg = self.session.feed_identities(msg, copy=False)
        try:
            msg = self.session.unpack_message(msg, content=True, copy=False)
        except:
            self.log.error("Invalid Message", exc_info=True)
            return
            
        
        header = msg['header']
        msg_id = header['msg_id']
        if self.check_aborted(msg_id):
            self.aborted.remove(msg_id)
            # is it safe to assume a msg_id will not be resubmitted?
            reply_type = msg['msg_type'].split('_')[0] + '_reply'
            reply_msg = self.session.send(stream, reply_type, 
                        content={'status' : 'aborted'}, parent=msg, ident=idents)
            return
        handler = self.shell_handlers.get(msg['msg_type'], None)
        if handler is None:
            self.log.error("UNKNOWN MESSAGE TYPE: %r"%msg['msg_type'])
        else:
            handler(stream, idents, msg)
    
    def start(self):
        #### stream mode:
        if self.control_stream:
            self.control_stream.on_recv(self.dispatch_control, copy=False)
            self.control_stream.on_err(printer)
        
        def make_dispatcher(stream):
            def dispatcher(msg):
                return self.dispatch_queue(stream, msg)
            return dispatcher
        
        for s in self.shell_streams:
            s.on_recv(make_dispatcher(s), copy=False)
            s.on_err(printer)
        
        if self.iopub_stream:
            self.iopub_stream.on_err(printer)
        
        #### while True mode:
        # while True:
        #     idle = True
        #     try:
        #         msg = self.shell_stream.socket.recv_multipart(
        #                     zmq.NOBLOCK, copy=False)
        #     except zmq.ZMQError, e:
        #         if e.errno != zmq.EAGAIN:
        #             raise e
        #     else:
        #         idle=False
        #         self.dispatch_queue(self.shell_stream, msg)
        #             
        #     if not self.task_stream.empty():
        #         idle=False
        #         msg = self.task_stream.recv_multipart()
        #         self.dispatch_queue(self.task_stream, msg)
        #     if idle:
        #         # don't busywait
        #         time.sleep(1e-3)

