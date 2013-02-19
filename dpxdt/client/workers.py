#!/usr/bin/env python
# Copyright 2013 Brett Slatkin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Workers for driving screen captures, perceptual diffs, and related work."""

import Queue
import heapq
import json
import logging
import subprocess
import sys
import threading
import time
import urllib2

# Local Libraries
import gflags
FLAGS = gflags.FLAGS


gflags.DEFINE_float(
    'polltime', 1.0,
    'How long to sleep between polling for work or subprocesses')

gflags.DEFINE_float(
    'fetch_frequency', 1.0,
    'Maximum number of fetches to make per second per thread.')



class Error(Exception):
    """Base class for exceptions in this module."""

class TimeoutError(Exception):
    """Subprocess has taken too long to complete and was terminated."""



class WorkItem(object):
    """Base work item that can be handled by a worker thread."""

    def __init__(self):
        self.error = None

    @staticmethod
    def _print_tree(obj):
        if isinstance(obj, dict):
            result = []
            for key, value in obj.iteritems():
                result.append("'%s': %s" % (key, WorkItem._print_tree(value)))
            return '{%s}' % ', '.join(result)
        else:
            value_str = repr(obj)
            if len(value_str) > 100:
                return '%s...%s' % (value_str[:100], value_str[-1])
            else:
                return value_str

    def __repr__(self):
        return '%s.%s(%s)' % (
            self.__class__.__module__,
            self.__class__.__name__,
            self._print_tree(self.__dict__))

    def check_result(self):
        # TODO: For WorkflowItems, remove generator.throw(*item.error) from
        # the stack trace since it's noise.
        if self.error:
            raise self.error[0], self.error[1], self.error[2]


class WorkerThread(threading.Thread):
    """Base worker thread that handles items one at a time."""

    def __init__(self, input_queue, output_queue):
        """Initializer.

        Args:
            input_queue: Queue this worker consumes work from.
            output_queue: Queue where this worker puts new work items, if any.
        """
        threading.Thread.__init__(self)
        self.daemon = True
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.interrupted = False
        self.polltime = FLAGS.polltime

    def run(self):
        while not self.interrupted:
            try:
                item = self.input_queue.get(True, self.polltime)
            except Queue.Empty:
                self.handle_nothing()
                continue

            try:
                next_item = self.handle_item(item)
            except Exception, e:
                item.error = sys.exc_info()
                logging.debug('%s error item=%r', self.worker_name, item)
                self.output_queue.put(item)
            else:
                logging.debug('%s processed item=%r', self.worker_name, item)
                if next_item:
                    self.output_queue.put(next_item)
            finally:
                self.input_queue.task_done()

    @property
    def worker_name(self):
        return '%s:%s' % (self.__class__.__name__, self.ident)

    def handle_nothing(self):
        """Runs whenever there are no items in the queue."""
        pass

    def handle_item(self, item):
        """Handles a single item.

        Args:
            item: WorkItem to process.

        Returns:
            A WorkItem that should go on the output queue. If None, then
            the provided work item is considered finished and no
            additional work is needed.
        """
        raise NotImplemented


class FetchItem(WorkItem):
    """Work item that is handled by fetching a URL."""

    def __init__(self, url, post=None, timeout_seconds=30):
        """Initializer.

        Args:
            url: URL to fetch.
            post: Optional. When supplied, a dictionary of post parameters to
                include in the request.
            timeout_seconds: Optional. How long until the fetch should timeout.
        """
        WorkItem.__init__(self)
        self.url = url
        self.post = post
        self.timeout_seconds = timeout_seconds
        self.status_code = None
        self.data = None
        self.headers = None
        self._data_json = None

    @property
    def json(self):
        """Returns de-JSONed data or None if it's a different content type."""
        if self._data_json:
            return self._data_json

        if not self.data or self.headers.gettype() != 'application/json':
            return None

        self._data_json = json.loads(self.data)
        return self._data_json


class FetchThread(WorkerThread):
    """Worker thread for fetching URLs."""

    def handle_item(self, item):
        start_time = time.time()
        data = None
        if item.post:
            data = urllib.urlencode(item.post)

        try:
            try:
                conn = urllib2.urlopen(
                    item.url, data=data, timeout=item.timeout_seconds)
                item.status_code = conn.getcode()
                item.headers = conn.info()
                if item.status_code == 200:
                    item.data = conn.read()
            except urllib2.HTTPError, e:
                item.status_code = e.code
            except urllib2.URLError:
                # TODO: Do something smarter here, like report a 400 error.
                pass

            return item
        finally:
            end_time = time.time()
            wait_duration = (1.0 / FLAGS.fetch_frequency) - (
                end_time - start_time)
            if wait_duration > 0:
                logging.debug('Rate limiting URL fetch for %f seconds',
                              wait_duration)
                time.sleep(wait_duration)


class ProcessItem(WorkItem):
    """Work item that is handled by running a subprocess."""

    def __init__(self, log_path, timeout_seconds=30):
        """Initializer.

        Args:
            log_path: Path to where output from this subprocess should be
                written.
            timeout_seconds: How long before the process should be force
                killed.
        """
        WorkItem.__init__(self)
        self.log_path = log_path
        self.timeout_seconds = timeout_seconds
        self.return_code = None


class ProcessThread(WorkerThread):
    """Worker thread that runs subprocesses."""

    def get_args(self, item):
        raise NotImplemented

    def handle_item(self, item):
        start_time = time.time()
        with open(item.log_path, 'w') as output_file:
            args = self.get_args(item)
            logging.debug('%s item=%r Running subprocess: %r',
                          self.worker_name, item, args)
            process = subprocess.Popen(
                args,
                stderr=subprocess.STDOUT,
                stdout=output_file,
                close_fds=True)

            while True:
                process.poll()
                if process.returncode is None:
                    now = time.time()
                    run_time = now - start_time
                    if run_time > item.timeout_seconds or self.interrupted:
                        process.kill()
                        raise TimeoutError(
                            'Sent SIGKILL to item=%r, pid=%s, run_time=%s' %
                            (item, process.pid, run_time))

                    time.sleep(FLAGS.polltime)
                    continue

                item.returncode = process.returncode

                return item


class TimerItem(WorkItem):
    """Work item for waiting some period of time before returning."""

    def __init__(self, delay_seconds):
        WorkItem.__init__(self)
        self.ready_time = time.time() + delay_seconds


class TimerThread(WorkerThread):
    """"Worker thread that tracks many timers."""

    def __init__(self, *args):
        """Initializer."""
        WorkerThread.__init__(self, *args)
        self.timers = []

    def handle_nothing(self):
        now = time.time()
        while self.timers:
            ready_time, _ = self.timers[0]
            wait_time = now - ready_time
            if wait_time <= 0:
                _, item = heapq.heappop(self.timers)
                self.output_queue.put(item)
            else:
                # Wait for new work up to the point that the earliest
                # timer is ready to fire.
                self.polltime = wait_time
                return

        # Nothing to do, use the default poll time.
        self.polltime = FLAGS.polltime

    def handle_item(self, item):
        heapq.heappush(self.timers, (item.ready_time, item))
        self.handle_nothing()


class WorkflowItem(WorkItem):
    """Work item for coordinating other work items.

    To use: Sub-class and override run(). Yield WorkItems you want processed
    as part of this workflow. Exceptions in child workflows will be reinjected
    into the run() generator at the yield point. Results will be available on
    the WorkItems returned by yield statements. Yield a list of WorkItems
    to do them in parallel. The first error encountered for the whole list
    will be raised if there's an exception.
    """

    def __init__(self, *args, **kwargs):
        WorkItem.__init__(self)
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.done = False
        self.root = False

    def run(self, *args, **kwargs):
        yield 'Yo dawg'


class Barrier(list):
    """Barrier for running multiple WorkItems in parallel."""

    def __init__(self, workflow, generator, work):
        """Initializer.

        Args:
            workflow: WorkflowItem instance this is for.
            generator: Current state of the WorkflowItem's generator.
            work: Next set of work to do. May be a single WorkItem object or
                a list or tuple that contains a set of WorkItems to run in
                parallel.
        """
        list.__init__(self)
        self.workflow = workflow
        self.generator = generator
        if isinstance(work, (list, tuple)):
            self[:] = list(work)
            self.was_list = True
        else:
            self[:] = [work]
            self.was_list = False
        self.remaining = len(self)
        self.error = None

    def get_item(self):
        """Returns the item to send back into the workflow generator."""
        if self.was_list:
            return self
        else:
            return self[0]

    def finish(self, item):
        """Marks the given item that is part of the barrier as done."""
        self.remaining -= 1
        if item.error and not self.error:
            self.error = item.error


class WorkflowThread(WorkerThread):
    """Worker thread for running workflows."""

    def __init__(self, input_queue, output_queue):
        """Initializer.

        Args:
            input_queue: Queue this worker consumes work from. These should be
                WorkflowItems to process, or any WorkItems registered with this
                class using the register() method.
            output_queue: Queue where this worker puts finished work items,
                if any.
        """
        WorkerThread.__init__(self, input_queue, output_queue)
        self.pending = {}
        self.work_map = {}
        self.worker_threads = []
        self.register(WorkflowItem, input_queue)

    # TODO: Implement drain, to let all existing work finish but no new work
    # allowed at the top of the funnel.

    def start(self):
        """Starts the coordinator thread and all related worker threads."""
        assert not self.interrupted
        for thread in self.worker_threads:
            thread.start()
        WorkerThread.start(self)

    def stop(self):
        """Stops the coordinator thread and all related threads."""
        if self.interrupted:
            return
        for thread in self.worker_threads:
            thread.interrupted = True
        self.interrupted = True
        for thread in self.worker_threads:
            thread.join()
        self.join()

    def register(self, work_type, queue):
        """Registers where work for a specific type can be executed.

        Args:
            work_type: Sub-class of WorkItem to register.
            queue: Queue instance where WorkItems of the work_type should be
                enqueued when they are yielded by WorkflowItems being run by
                this worker.
        """
        self.work_map[work_type] = queue

    def handle_item(self, item):
        if isinstance(item, WorkflowItem) and not item.done:
            workflow = item
            generator = item.run(*item.args, **item.kwargs)
            item = None
        else:
            barrier = self.pending.pop(item)
            barrier.finish(item)
            if barrier.remaining and not barrier.error:
                return
            item = barrier.get_item()
            workflow = barrier.workflow
            generator = barrier.generator

        while True:
            logging.debug('Transitioning workflow=%r, generator=%r, item=%r',
                          workflow, generator, item)
            try:
                if item is not None and item.error:
                    next_item = generator.throw(*item.error)
                else:
                    next_item = generator.send(item)
            except StopIteration:
                workflow.done = True
                if workflow.root:
                    return workflow
                else:
                    self.input_queue.put(workflow)
                    return
            except Exception, e:
                # Sub-workflow re-raised an exception. Reinject it into the
                # workflow so a pending parent can catch it.
                workflow.done = True
                workflow.error = sys.exc_info()
                if workflow.root:
                    return workflow
                else:
                    self.input_queue.put(workflow)
                    return

            # If a returned barrier is empty, immediately progress the
            # workflow.
            barrier = Barrier(workflow, generator, next_item)
            if barrier:
                break
            else:
                item = None

        for item in barrier:
            if isinstance(item, WorkflowItem):
                target_queue = self.input_queue
            else:
                target_queue = self.work_map[type(item)]
            self.pending[item] = barrier
            target_queue.put(item)


def GetCoordinator():
    """Creates a coordinator and returns it."""
    fetch_queue = Queue.Queue()
    timer_queue = Queue.Queue()
    workflow_queue = Queue.Queue()
    complete_queue = Queue.Queue()

    coordinator = WorkflowThread(workflow_queue, complete_queue)
    coordinator.register(FetchItem, fetch_queue)

    # TODO: Make number of threads configurable.
    # TODO: Enable multiple coodinator threads.
    coordinator.worker_threads = [
        FetchThread(fetch_queue, workflow_queue),
        FetchThread(fetch_queue, workflow_queue),
        TimerThread(timer_queue, workflow_queue),
    ]

    return coordinator
