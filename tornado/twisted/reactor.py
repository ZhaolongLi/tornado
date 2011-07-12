# Author: Ovidiu Predescu
# Date: July 2011
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
A twisted-style reactor for the Tornado IOLoop.

To use it, add the following to your twisted application:

import tornado.twisted.reactor
tornado.twisted.reactor.install()
from twisted.internet import reactor
"""

import errno, functools, sys
import time

from twisted.internet.base import DelayedCall
from twisted.internet.posixbase import PosixReactorBase
from twisted.internet.interfaces import \
    IReactorFDSet, IDelayedCall, IReactorTime

from zope.interface import implements

import tornado
import tornado.ioloop
from tornado.ioloop import IOLoop

class TornadoDelayedCall(object):
    """
    DelayedCall object for Tornado.
    """
    implements(IDelayedCall)

    def __init__(self, reactor, seconds, f, *args, **kw):
        self._reactor = reactor
        self._func = functools.partial(f, *args, **kw)
        self._time = self._reactor.seconds() + seconds
        self._timeout = self._reactor._ioloop.add_timeout(self._time,
                                                          self._called)
        self._active = True

    def _called(self):
        self._active = False
        self._reactor._removeDelayedCall(self)
        try:
            self._func()
        except:
            print "reactor.py _called caught exception: %s" % sys.exc_info()[0]

    def getTime(self):
        return self._time

    def cancel(self):
        self._active = False
        self._reactor._ioloop.remove_timeout(self._timeout)
        self._reactor._removeDelayedCall(self)

    def delay(self, seconds):
        self._reactor._ioloop.remove_timeout(self._timeout)
        self._time += seconds
        self._timeout = self._reactor._ioloop.add_timeout(self._time,
                                                          self._called)

    def reset(self, seconds):
        self._reactor._ioloop.remove_timeout(self._timeout)
        self._time = self._reactor.seconds() + seconds
        self._timeout = self._reactor._ioloop.add_timeout(self._time,
                                                          self._called)

    def active(self):
        return self._active

class TornadoReactor(PosixReactorBase):
    """
    Twisted style reactor for Tornado.
    """
    implements(IReactorTime, IReactorFDSet)

    def __init__(self, ioloop):
        if not ioloop:
            ioloop = tornado.ioloop.IOLoop.instance()
        self._ioloop = ioloop
        self._readers = {}
        self._writers = {}
        self._fds = {} # a map of fd to a (reader, writer) tuple
        self._delayedCalls = {}
        # self._waker = None
        PosixReactorBase.__init__(self)

    # IReactorTime
    def seconds(self):
        return time.time()

    def callLater(self, seconds, f, *args, **kw):
        dc = TornadoDelayedCall(self, seconds, f, *args, **kw)
        self._delayedCalls[dc] = True
        return dc

    def getDelayedCalls(self):
        return [x for x in self._delayedCalls if x._active]

    def _removeDelayedCall(self, dc):
        if dc in self._delayedCalls:
            del self._delayedCalls[dc]

    # IReactorThreads
    def callFromThread(self, f, *args, **kw):
        """
        See L{twisted.internet.interfaces.IReactorThreads.callFromThread}.
        """
        assert callable(f), "%s is not callable" % f
        p = functools.partial(f, *args, **kw)
        self._ioloop.add_callback(p)

    # We don't need the waker code from the super class, Tornado uses
    # its own waker.
    def installWaker(self):
        pass

    def wakeUp(self):
        pass

    # IReactorFDSet
    def _invoke_callback(self, fd, events):
        (reader, writer) = self._fds[fd]
        if events | IOLoop.READ and reader:
            reader.doRead()
        if events | IOLoop.WRITE and writer:
            writer.doWrite()

    def addReader(self, reader):
        """
        Add a FileDescriptor for notification of data available to read.
        """
        self._readers[reader] = True
        fd = reader.fileno()
        if fd in self._fds:
            (_, writer) = self._fds[fd]
            self._fds[fd] = (reader, writer)
            if writer:
                # We already registered this fd for write events,
                # update it for read events as well.
                self._ioloop.update_handler(fd, IOLoop.READ | IOLoop.WRITE)
        else:
            self._fds[fd] = (reader, None)
            self._ioloop.add_handler(fd, self._invoke_callback, IOLoop.READ)

    def addWriter(self, writer):
        """
        Add a FileDescriptor for notification of data available to write.
        """
        self._writers[writer] = True
        fd = writer.fileno()
        if fd in self._fds:
            (reader, _) = self._fds[fd]
            self._fds[fd] = (reader, writer)
            if reader:
                # We already registered this fd for read events,
                # update it for write events as well.
                self._ioloop.update_handler(fd, IOLoop.READ | IOLoop.WRITE)
        else:
            self._fds[fd] = (None, writer)
            self._ioloop.add_handler(fd, self._invoke_callback, IOLoop.WRITE)

    def removeReader(self, reader):
        """
        Remove a Selectable for notification of data available to read.
        """
        fd = reader.fileno()
        if reader in self._readers:
            del self._readers[reader]
            (_, writer) = self._fds[fd]
            if writer:
                # We have a writer so we need to update the IOLoop for
                # write events only.
                self._fds[fd] = (None, writer)
                self._ioloop.update_handler(fd, IOLoop.WRITE)
            else:
                # Since we have no writer registered, we remove the
                # entry from _fds and unregister the handler from the
                # IOLoop
                del self._fds[fd]
                self._ioloop.remove_handler(fd)

    def removeWriter(self, writer):
        """
        Remove a Selectable for notification of data available to write.
        """
        fd = writer.fileno()
        if writer in self._writers:
            del self._writers[writer]
            (reader, _) = self._fds[fd]
            if reader:
                # We have a reader so we need to update the IOLoop for
                # read events only.
                self._fds[fd] = (reader, None)
                self._ioloop.update_handler(fd, IOLoop.READ)
            else:
                # Since we have no reader registered, we remove the
                # entry from the _fds and unregister the handler from
                # the IOLoop.
                del self._fds[fd]
                self._ioloop.remove_handler(fd)

    def removeAll(self):
        return self._removeAll(self._readers, self._writers)

    def getReaders(self):
        return self._readers.keys()

    def getWriters(self):
        return self._writers.keys()

    def stop(self):
        """
        Implement L{IReactorCore.stop}.
        """
        PosixReactorBase.stop(self)
        self.runUntilCurrent()
        self._ioloop.stop()

    def crash(self):
        PosixReactorBase.crash(self)
        self.runUntilCurrent()
        self._ioloop.stop()

    def doIteration(self, delay):
        raise NotImplementedError("doIteration")

    def mainLoop(self):
        self.running = True
        self._ioloop.start()

def install(ioloop=None):
    """
    Install the Tornado reactor.
    """
    if not ioloop:
        ioloop = tornado.ioloop.IOLoop.instance()
    reactor = TornadoReactor(ioloop)
    from twisted.internet.main import installReactor
    installReactor(reactor)
    return reactor
