#!/usr/bin/python
#
# Distributed work queue operations using mpi4py.
#
# Copyright (C) 2020 Ivan Raikov and distwq authors.
# 
# Based on mpi.py from the pyunicorn project.
# Copyright (C) 2008--2019 Jonathan F. Donges and pyunicorn authors
# URL: <http://www.pik-potsdam.de/members/donges/software>
# License: BSD (3-clause)
#
# Please acknowledge and cite the use of this software and its authors
# when results are used in publications or published elsewhere.
#
# You can use the following reference:
# J.F. Donges, J. Heitzig, B. Beronov, M. Wiedermann, J. Runge, Q.-Y. Feng,
# L. Tupikina, V. Stolbova, R.V. Donner, N. Marwan, H.A. Dijkstra,
# and J. Kurths, "Unified functional network and nonlinear time series analysis
# for complex systems science: The pyunicorn package"

"""
Distributed work queue operations using mpi4py.

Allows for easy parallelization in controller/worker mode with one
controller submitting function or method calls to workers.  Supports
multiple ranks per worker (collective workers). Uses mpi4py if
available, otherwise processes calls sequentially in one process.

"""
#
#  Imports
#

import sys, signal, importlib, time, traceback, logging, uuid
from enum import Enum, IntEnum
import numpy as np

class CollectiveMode(IntEnum):
    Gather = 1

class MessageTag(IntEnum):
    READY = 0
    DONE = 1
    TASK = 2
    EXIT = 3

    
logger = logging.getLogger(__name__)

# try to get the communicator object to see whether mpi is available:
try:
    from mpi4py import MPI
    comm = MPI.COMM_WORLD
    has_mpi = True
except ImportError:
    has_mpi = False

def mpi_excepthook(type, value, traceback):
    """

    :param type:
    :param value:
    :param traceback:
    :return:
    """
    sys_excepthook(type, value, traceback)
    sys.stderr.flush()
    MPI.COMM_WORLD.Abort(1)

if has_mpi:
    sys_excepthook = sys.excepthook
    sys.excepthook = mpi_excepthook

my_args = sys.argv[sys.argv.index('-')+1:] if '-' in sys.argv else None

# message types
tag_ctrl_to_worker = 1
tag_worker_to_ctrl = 2

# initialize:
workers_available = True
spawned = False
if has_mpi:
    spawned = (my_args[0] == 'distwq:spawned') if my_args is not None else False
    size = comm.size
    rank = comm.rank
    is_controller = (not spawned) and (rank == 0)
    if size < 2:
        workers_available = False
else:
    size = 1
    rank = 0
    is_controller = True

    
is_worker = not is_controller
n_workers = size - 1
start_time = time.time()




class MPIController(object):

    def __init__(self, comm):
        
        size = comm.size
        rank = comm.rank

        self.comm = comm
        self.workers_available = True if size > 1 else False
        
        self.count = 0

        self.total_time_est = np.ones(size)
        """
        (numpy array of ints)
        total_time_est[i] is the current estimate of the total time
        MPI worker i will work on already submitted calls.
        On worker i, only total_time_est[i] is available.
        """
        self.total_time_est[0] = np.inf
        self.result_queue = []
        self.task_queue = []
        self.ready_workers = []
        """(list) ids of submitted calls"""
        self.assigned = {}
        """
        (dictionary)
        assigned[id] is the worker assigned to the call with that id.
        """
        self.worker_queue = [[] for i in range(0, size)]
        """
        (list of lists)
        worker_queue[i] contains the ids of calls assigned to worker i.
        """
        self.n_processed = np.zeros(size).astype(np.int)
        """
        (list of ints)
        n_processed[rank] is the total number of calls processed by MPI node rank.
        On worker i, only total_time[i] is available.
        """
        self.total_time = np.zeros(size).astype(np.float32)
        """
        (list of floats)
        total_time[rank] is the total wall time until that node finished its last
        call.  On worker i, only total_time[i] is available.
        """
        self.results = {}
        """
        (dictionary)
        if mpi is not available, the result of submit_call(..., id=a) will be
        cached in results[a] until get_result(a).
        """
        self.stats = []

        """
        (list of dictionaries)
        stats[id] contains processing statistics for the last call with this id. Keys:
        
        - "id": id of the call
        - "rank": MPI node who processed the call
        - "this_time": wall time for processing the call
        - "time_over_est": quotient of actual over estimated wall time
        - "n_processed": no. of calls processed so far by this worker, including this
        - "total_time": total wall time until this call was finished
        """

    def recv(self):
        """
        Process incoming messages.
        """
        if self.comm.Iprobe(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG):

            status = MPI.Status()
            data = self.comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, status=status)
            worker = status.Get_source()
            tag = status.Get_tag()
            if tag == MessageTag.READY:
                self.ready_workers.append(worker)
            elif tag == MessageTag.DONE:
                task_id, results, stats = data
                self.results[task_id] = results
                self.stats.append(stats)
                self.n_processed[worker] = stats["n_processed"]
                self.total_time[worker] = stats["total_time"]
                self.task_queue.remove(task_id)
                self.result_queue.append(task_id)
                self.worker_queue[worker].remove(task_id)
                self.assigned.pop(task_id)
            else:
                raise RuntimeError(f"MPI controller : invalid message tag {tag}")
        else:
            time.sleep(1)
        
    def submit_call(self, name_to_call, args=(), kwargs={},
                    module_name="__main__", time_est=1, task_id=None):
        """
        Submit a call for parallel execution.

        If called by the controller and workers are available, the call is submitted
        to a worker for asynchronous execution.

        If called by a worker or if no workers are available, the call is instead
        executed synchronously on this MPI node.

        **Examples:**

            1. Provide ids and time estimate explicitly:

               .. code-block:: python

                  for n in range(0,10):
                      distwq.submit_call("doit", (n,A[n]), id=n, time_est=n**2)

                  for n in range(0,10):
                      result[n] = distwq.get_result(n)

            2. Use generated ids stored in a list:

               .. code-block:: python

                  for n in range(0,10):
                      ids.append(distwq.submit_call("doit", (n,A[n])))

                  for n in range(0,10):
                      results.append(distwq.get_result(ids.pop()))

            3. Ignore ids altogether:

               .. code-block:: python

                  for n in range(0,10):
                      distwq.submit_call("doit", (n,A[n]))

                  for n in range(0,10):
                      results.append(distwq.get_next_result())

            4. Call a module function and use keyword arguments:

               .. code-block:: python

                  distwq.submit_call("solve", (), {"a":a, "b":b},
                                       module="numpy.linalg")


        :arg str name_to_call: name of callable object (usually a function or
            static method of a class) as contained in the namespace specified
            by module.
        :arg tuple args: the positional arguments to provide to the callable
            object.  Tuples of length 1 must be written (arg,).  Default: ()
        :arg dict kwargs: the keyword arguments to provide to the callable
            object.  Default: {}
        :arg str module: optional name of the imported module or submodule in
            whose namespace the callable object is contained. For objects
            defined on the script level, this is "__main__", for objects
            defined in an imported package, this is the package name. Must be a
            key of the dictionary sys.modules (check there after import if in
            doubt).  Default: "__main__"
        :arg float time_est: estimated relative completion time for this call;
            used to find a suitable worker. Default: 1
        :type id: object or None
        :arg  id: unique id for this call. Must be a possible dictionary key.
            If None, a random id is assigned and returned. Can be re-used after
            get_result() for this is. Default: None
        :type worker: int > 0 and < comm.size, or None
        :arg  worker: optional no. of worker to assign the call to. If None, the
            call is assigned to the worker with the smallest current total time
            estimate. Default: None
        :return object: id of call, to be used in get_result().
        """
        if task_id is None:
            task_id = self.count
            self.count += 1
        if task_id in self.assigned:
            raise RuntimeError("id ", str(task_id), " already in queue!")
        if self.workers_available:
            while True:
                self.recv()
                if len(self.ready_workers) > 0:
                    ready_total_time_est = np.asarray([self.total_time_est[worker] for worker in self.ready_workers])
                    worker = self.ready_workers[np.argmin(ready_total_time_est)]
                    # send name to call, args, time_est to worker:
                    logger.info(f"MPI controller : assigning call with id {task_id} to worker "
                                    f"{worker}: {name_to_call} {args} {kwargs} ...")
                    req = self.comm.isend((name_to_call, args, kwargs, module_name, time_est, task_id),
                                          dest=worker, tag=MessageTag.TASK)
                    req.wait()
                    self.ready_workers.remove(worker)
                    break

        else:
            # perform call on this rank if no workers are available:
            worker = 0
            logger.info(f"MPI controller : calling {name_to_call} {args} {kwargs} "
                        "...")
            try:
                if module_name not in sys.modules:
                    importlib.import_module(module_name)
                object_to_call = eval(name_to_call,
                                      sys.modules[module_name].__dict__)
            except NameError:
                logger.error(str(sys.modules[module_name].__dict__.keys()))
                raise
            call_time = time.time()
            self.results[task_id] = object_to_call(*args, **kwargs)
            this_time = time.time() - call_time
            self.n_processed[0] += 1
            self.total_time[0] = time.time() - start_time
            self.stats.append({"id":task_id, "rank": 0,
                               "this_time": this_time,
                               "time_over_est": this_time / time_est,
                               "n_processed": self.n_processed[0],
                               "total_time": self.total_time[0]})

        self.total_time_est[worker] += time_est
        self.task_queue.append(task_id)
        self.worker_queue[worker].append(task_id)
        self.assigned[task_id] = worker
        return task_id

    def get_result(self, task_id):
        """
        Return result of earlier submitted call.

        Can only be called by the controller.

        If the call is not yet finished, waits for it to finish.
        Results should be collected in the same order as calls were submitted.
        For each worker, the results of calls assigned to that worker must be
        collected in the same order as those calls were submitted.
        Can only be called once per call.

        :type id: object
        :arg  id: id of an earlier submitted call, as provided to or returned
                  by submit_call().

        :rtype:  object
        :return: return value of call.
        """
        if task_id in self.results:
            return task_id, self.results[task_id]
        source = self.assigned[task_id]
        if self.workers_available:
            if self.worker_queue[source][0] != task_id:
                raise RuntimeError("get_result(" + str(task_id)
                                   + ") called before get_result("
                                   + str(worker_queue[source][0]) + ")")
            logger.info(f"MPI controller : retrieving result for call with id {task_id} "
                        f"from worker {source} ...")
            
            while not (task_id in self.results):
                self.recv()

            logger.info(f"MPI controller : received result for call with id {task_id} "
                        f"from worker {source}.")
            
        else:
            logger.info(f"MPI controller : returning result for call with id {task_id} "
                        "...")
        result = self.results[task_id]
        self.result_queue.remove(task_id)
        return task_id, result

    def get_next_result(self):
        """
        Return result of next earlier submitted call whose result has not yet
        been obtained.

        Can only be called by the controller.

        If the call is not yet finished, waits for it to finish.

        :rtype:  object
        :return: id, return value of call, or None of there are no more calls in
                 the queue.
        """
        if len(self.result_queue) > 0:
            task_id = self.result_queue.pop(0)
            return task_id, self.results[task_id]
        elif len(self.task_queue) > 0:
            task_id = self.task_queue[0]
            return self.get_result(task_id)
        else:
            return None

    def info(self):
        """
        Print processing statistics.

        Can only be called by the controller.
        """

        call_times = np.array([s["this_time"] for s in self.stats])
        call_quotients = np.array([s["time_over_est"] for s in self.stats])

        if self.workers_available:
            worker_quotients = self.total_time/self.total_time_est
            print("\n"
                  "MPI run statistics\n"
                  "     =====================\n"
                  "     results collected:         "
                  f"{self.n_processed[1:].sum()}\n"
                  "     results not yet collected: "
                  f"{len(self.task_queue)}\n"
                  "     total reported time:       "
                  f"{call_times.sum()}\n"
                  "     mean time per call:        "
                  f"{call_times.mean()}\n"
                  "     std.dev. of time per call: "
                  f"{call_times.std()}\n"
                  "     coeff. of var. of actual over estd. time per call: "
                  f"{call_quotients.std()/call_quotients.mean()}\n"
                  "     workers:                      "
                  f"{n_workers}\n"
                  "     mean calls per worker:        "
                  f"{self.n_processed[1:].mean()}\n"
                  "     std.dev. of calls per worker: "
                  f"{self.n_processed[1:].std()}\n"
                  "     min calls per worker:         "
                  f"{self.n_processed[1:].min()}\n"
                  "     max calls per worker:         "
                  f"{self.n_processed[1:].max()}\n"
                  "     mean time per worker:        "
                  f"{self.total_time.mean()}\n"
                  "     std.dev. of time per worker: "
                  f"{self.total_time.std()}\n"
                  "     coeff. of var. of actual over estd. time per worker: "
                  f"{worker_quotients.std()/worker_quotients.mean()}\n")
        else:
            print("\n"
                  "MPI run statistics\n"
                  "     =====================\n"
                  "     results collected:         "
                  f"{self.n_processed[0]}\n"
                  "     results not yet collected: "
                  f"{len(self.task_queue)}\n"
                  "     total reported time:       "
                  f"{call_times.sum()}\n"
                  "     mean time per call:        "
                  f"{call_times.mean()}\n"
                  "     std.dev. of time per call: "
                  f"{call_times.std()}\n"
                  "     coeff. of var. of actual over estd. time per call: "
                  f"{call_quotients.std()/call_quotients.mean()}\n")

    def exit(self):
        """
        Tell all workers to exit.

        Can only be called by the controller.
        """
        if self.workers_available:
            while self.get_next_result() is not None:
                pass
            # tell workers to exit:
            for worker in range(1, n_workers+1):
                logger.info(f"MPI controller : telling worker {worker} "
                            "to exit...")
                req = comm.isend(None, dest=worker, tag=MessageTag.EXIT)

                
    def abort(self):
        """
        Abort execution on all MPI nodes immediately.

        Can be called by controller and workers.
        """
        traceback.print_exc()
        logger.error("MPI controller : aborting...")
        self.comm.Abort()

        
class MPIWorker(object):        

    def __init__(self, comm):
        self.comm = comm
        self.total_time_est = np.zeros(size)*np.nan
        self.total_time_est[rank] = 0
        self.n_processed = np.zeros(size)*np.nan
        self.n_processed[rank] = 0
        self.total_time = np.zeros(size)*np.nan
        self.total_time[rank] = 0
        self.stats = []
        logger.info("MPI worker %d: initialized." % self.comm.rank)
        
    def serve(self):
        """
        Serve submitted calls until told to finish.

        Call this function if workers need to perform initialization
        different from the controller, like this:

        >>> def workerfun(worker):
        >>>     do = whatever + initialization - is * necessary
        >>>     worker.serve()
        >>>     do = whatever + cleanup - is * necessary

        If you don't define workerfun(), serve() will be called automatically by
        run().
        """
        size = self.comm.size
        rank = self.comm.rank

        logger.info("MPI worker %d: waiting for calls." % rank)
            
        # wait for orders:
        ready = True
        status = MPI.Status()
        exit_flag = False
        while not exit_flag:
            # signal the controller this worker is ready
            if ready:
                req = self.comm.isend(None, dest=0, tag=MessageTag.READY)
                req.wait()
            
            # get next task from queue:
            if self.comm.Iprobe(source=0, tag=MPI.ANY_TAG):
                data = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
                tag = status.Get_tag()
                
                # TODO: add timeout and check whether controller lives!
                object_to_call = None
                if tag == MessageTag.EXIT:
                    logger.info("MPI worker %d: exiting..." % rank)
                    exit_flag = True
                    break
                elif tag == MessageTag.TASK:
                    try:
                        (name_to_call, args, kwargs, module, time_est, task_id) = data
                        if module not in sys.modules:
                            importlib.import_module(module)
                        object_to_call = eval(name_to_call,
                                              sys.modules[module].__dict__)
                    except NameError:
                        logger.error(str(sys.modules[module].__dict__.keys()))
                        raise
                else:
                    raise RuntimeError('MPI worker %d: unknown message tag' % rank)
                self.total_time_est[rank] += time_est
                call_time = time.time()
                result = object_to_call(*args, **kwargs)
                this_time = time.time() - call_time
                self.n_processed[rank] += 1
                self.stats.append({"id": task_id, "rank": rank,
                                   "this_time": this_time,
                                   "time_over_est": this_time / time_est,
                                   "n_processed": self.n_processed[rank],
                                   "total_time": time.time() - start_time})
                req = comm.isend((task_id, result, self.stats[-1]), dest=0, tag=MessageTag.DONE)
                req.wait()
                ready = True
            else:
                ready = False
                time.sleep(1)
    def abort(self):
        rank = self.comm.rank
        traceback.print_exc()
        logger.info("MPI worker %d: aborting..." % rank)
        comm.Abort()

class MPICollectiveWorker(object):

    def __init__(self, comm, worker_id, collective_mode=CollectiveMode.Gather):
        self.collective_mode = collective_mode
        self.worker_id = worker_id
        self.comm = comm
        self.parent_comm = self.comm.Get_parent()
        assert self.parent_comm != MPI.COMM_NULL
        self.merged_comm = self.parent_comm.Merge(True)

        self.total_time_est = np.zeros(size)*np.nan
        self.total_time_est[rank] = 0
        self.n_processed = np.zeros(size)*np.nan
        self.n_processed[rank] = 0
        self.total_time = np.zeros(size)*np.nan
        self.total_time[rank] = 0
        self.stats = []
        
    def serve(self):
        """
        Serve submitted calls until told to finish. Tasks are
        obtained via scatter and results are returned via gather, 
        i.e. all collective workers spawned by a CollectiveBroker 
        will participate in these collective calls.

        Call this function if workers need to perform initialization
        different from the controller, like this:

        >>> def workerfun(worker):
        >>>     do = whatever + initialization - is * necessary
        >>>     worker.serve()
        >>>     do = whatever + cleanup - is * necessary

        If you don't define workerfun(), serve() will be called automatically by
        run().
        """

        size = self.parent_comm.size
        rank = self.parent_comm.rank
        merged_rank = self.merged_comm.Get_rank()
        merged_size = self.merged_comm.Get_size()
        logger.info("MPI collective worker %d-%d: waiting for calls." % (self.worker_id, rank))

        # wait for orders:
        while True:
            logger.info("MPI collective worker %d-%d: getting next task from queue..." % (self.worker_id, rank))
            # get next task from queue:
            req = self.merged_comm.Ibarrier()
            (name_to_call, args, kwargs, module, time_est, task_id) = \
                self.merged_comm.scatter(None, root=0)
            req.wait()
            logger.info("MPI collective worker %d-%d: received next task from queue." % (self.worker_id, rank))
            # TODO: add timeout and check whether controller lives!
            if name_to_call == "exit":
                logger.info("MPI collective worker %d-%d: exiting..." % (self.worker_id, rank))
                self.merged_comm.Disconnect()
                self.parent_comm.Disconnect()
                break
            try:
                if module not in sys.modules:
                    importlib.import_module(module)
                object_to_call = eval(name_to_call,
                                      sys.modules[module].__dict__)
            except NameError:
                logger.error(str(sys.modules[module].__dict__.keys()))
                raise
            self.total_time_est[rank] += time_est
            call_time = time.time()
            result = object_to_call(*args, **kwargs)
            this_time = time.time() - call_time
            self.n_processed[rank] += 1
            self.stats.append({"id": task_id,
                               "rank": merged_rank,
                               "this_time": this_time,
                               "time_over_est": this_time / time_est,
                               "n_processed": self.n_processed[rank],
                               "total_time": time.time() - start_time})
            if self.collective_mode == CollectiveMode.Gather:
                req = self.merged_comm.Ibarrier()
                self.merged_comm.gather((result, self.stats[-1]), root=0)
                req.wait()
            else:
                raise RuntimeError("MPICollectiveWorker: unknown collective mode")
                

    def abort(self):
        rank = self.comm.rank
        traceback.print_exc()
        logger.info("MPI collective worker %d-%d: aborting..." % (self.worker_id, rank))
        comm.Abort()

        
class MPICollectiveBroker(object):        

    def __init__(self, comm, sub_comm, is_worker=False, collective_mode=CollectiveMode.Gather):
        logger.info('MPI collective broker %d starting' % (rank-1))
        assert(not spawned)
        self.collective_mode=collective_mode
        self.comm = comm
        self.sub_comm = sub_comm
        self.merged_comm = sub_comm.Merge(False)
        self.total_time_est = np.zeros(size)*np.nan
        self.total_time_est[rank] = 0
        self.n_processed = np.zeros(size)*np.nan
        self.n_processed[rank] = 0
        self.total_time = np.zeros(size)*np.nan
        self.total_time[rank] = 0
        self.stats = []
        self.is_worker = is_worker
        
    def serve(self):
        """
        Broker and serve submitted calls until told to finish. A task
        is received from the controller and sent to all collective
        workers associated with this broker via scatter.

        Call this function if workers need to perform initialization
        different from the controller, like this:

        >>> def workerfun(worker):
        >>>     do = whatever + initialization - is * necessary
        >>>     worker.serve()
        >>>     do = whatever + cleanup - is * necessary

        If you don't define workerfun(), serve() will be called automatically by
        run().
        """
        size = self.comm.size
        rank = self.comm.rank
        merged_rank = self.merged_comm.Get_rank()
        merged_size = self.merged_comm.Get_size()

        logger.info("MPI worker broker %d: waiting for calls." % (rank-1))
            
        # wait for orders:
        while True:
            # signal the controller this worker is ready
            req = self.comm.isend(None, dest=0, tag=MessageTag.READY)
            req.wait()
            logger.info("MPI collective broker %d: getting next task from controller..." % (rank-1))

            while True:
                msg = self.recv()
                if msg is not None:
                    tag, data = msg
                    break

            logger.info("MPI collective broker %d: received message from controller..." % (rank-1))

            if tag == MessageTag.EXIT:
                logger.info("MPI worker broker %d: exiting..." % (rank-1))
                req = self.merged_comm.Ibarrier()
                self.merged_comm.scatter([("exit", (), {}, "", 0, 0)]*merged_size, root=0)
                req.wait()
                self.merged_comm.Disconnect()
                self.sub_comm.Disconnect()
                break
            elif tag == MessageTag.TASK:
                (name_to_call, args, kwargs, module, time_est, task_id) = data
            else:
                raise RuntimeError('MPI collective broker: unknown message tag')
                 
            logger.info("MPI collective broker %d: sending task %s to workers..." % (rank-1, str(task_id)))
            req = self.merged_comm.Ibarrier()
            self.merged_comm.scatter([(name_to_call, args, kwargs, module, time_est, task_id)]*merged_size,
                                     root=merged_rank)
            req.wait()
            logger.info("MPI collective broker %d: sending task complete." % (rank-1))

            self.total_time_est[rank] += time_est
            if self.is_worker:
                call_time = time.time()
                result = object_to_call(*args, **kwargs)
                this_time = time.time() - call_time
                self.n_processed[rank] += 1
                this_stat = {"id": task_id,
                             "rank": merged_rank,
                             "this_time": this_time,
                             "time_over_est": this_time / time_est,
                             "n_processed": self.n_processed[rank],
                             "total_time": time.time() - start_time}
            else:
                result = None
                this_stat = None
                this_time = 0
                
            if this_stat is not None:
                self.stats.append(this_stat)

            logger.info("MPI collective broker %d: gathering data from workers..." % (rank-1))
            if self.collective_mode == CollectiveMode.Gather:
                req = self.merged_comm.Ibarrier()
                sub_data = self.merged_comm.gather((result, this_stat), root=merged_rank)
                req.wait()
                results = [result for result, stat in sub_data if result is not None]
                stats = [stat for result, stat in sub_data if result is not None]
            else:
                raise RuntimeError('MPICollectiveBroker: unknown collective mode')
            logger.info("MPI collective broker %d: gathered %s results from workers..." % (rank-1, len(results)))
            stat_times = np.asarray([stat["this_time"] for stat in stats])
            max_time = np.argmax(stat_times)
            stat = stats[max_time]
            logger.info("MPI collective broker %d: sending results to controller..." % (rank-1))
            req = self.comm.isend((task_id, results, stat), dest=0, tag=MessageTag.DONE)
            req.wait()

    def recv(self):
        status = MPI.Status()
        if self.comm.Iprobe(source=0, tag=MPI.ANY_TAG):
            # get next task from controller queue:
            data = self.comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
            tag = status.Get_tag()
            return tag, data
        else:
            time.sleep(1)
            return None
            


    def abort(self):
        rank = self.comm.rank
        traceback.print_exc()
        logger.info("MPI worker broker %d: aborting..." % rank)
        comm.Abort()



def run(fun_name=None, module_name='__main__', verbose=False, spawn_workers=False, nprocs_per_worker=1, broker_is_worker=False, args=()):
    """
    Run in controller/worker mode until fun(controller/worker) finishes.

    Must be called on all MPI nodes.

    On the controller, run() calls fun_name() and returns when fun_name() returns.

    On each worker, run() calls fun() if that is defined, or calls serve()
    otherwise, and returns when fun() returns, or when fun() returns on
    the controller, or when controller calls exit().

    :arg string module_name: module where fun_name is located
    :arg bool verbose: whether processing information should be printed.
    :arg bool spawn_workers: whether to spawn separate worker processes via MPI_Spawn
    :arg int nprocs_per_worker: how many processes per worker
    :arg broker_is_worker: when spawn_worker is True or nprocs_per_worker > 1, MPI_Spawn will be used to create workers, 
    and a CollectiveBroker object is used to relay tasks and results between controller and worker.
    When broker_is_worker is true, the broker also participates in serving tasks, otherwise it only 
    relays calls.
    :arg args: additional args to pass to fun
 

    """

    if verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARN)

    assert nprocs_per_worker > 0
    assert not spawned
    fun = None
    if fun_name is not None:
        if module_name not in sys.modules:
            importlib.import_module(module_name)
        fun = eval(fun_name, sys.modules[module_name].__dict__)

    if has_mpi:  # run in mpi mode
        if is_controller:  # I'm the controller
            assert(fun is not None)
            controller = MPIController(comm)
            signal.signal(signal.SIGINT, lambda signum, frame: controller.abort())
            try:  # put everything in a try block to be able to exit!
                fun(controller, *args)
            except ValueError:
                controller.abort()
            controller.exit()
        else:  # I'm a worker or a broker
            if (n_workers > 0) and (nprocs_per_worker > 1):
                spawn_workers = True
            if spawn_workers and (nprocs_per_worker==1) and broker_is_worker:
                raise RuntimeException("distwq.run: cannot spawn workers when nprocs_per_worker=1 and broker_is_worker is set to True")
            if spawn_workers:
                arglist = ['-m', 'distwq', '-', 'distwq:spawned', '%d' % (rank-1), '%d' % (1 if verbose else 0)]
                if fun is not None:
                    arglist += [str(fun_name), str(module_name)]
                sub_comm = MPI.COMM_SELF.Spawn(sys.executable, args=arglist,
                                                maxprocs=nprocs_per_worker-1 
                                                   if broker_is_worker else nprocs_per_worker)
                if fun is not None:
                    sub_comm.bcast(args, root=MPI.ROOT)
                broker=MPICollectiveBroker(comm, sub_comm, is_worker=broker_is_worker)
                if broker_is_worker and (fun is not None):
                    fun(broker, *args)
                broker.serve()
            else:
                worker = MPIWorker(comm)
                if fun is not None:
                    fun(worker, *args)
                worker.serve()
    else:  # run as single processor
        assert(fun is not None)
        logger.info("MPI controller : not available, running as a single process.")
        controller = MPIController()
        fun(controller, *args)
        logger.info("MPI controller : finished.")

        
if __name__ == '__main__':
    if is_worker:
        worker_id = int(my_args[1])
        verbose_flag = int(my_args[2])
        verbose = True if verbose_flag == 1 else False
        if verbose:
            logging.basicConfig(level=logging.INFO)
        else:
            logging.basicConfig(level=logging.WARN)
        logger.info('MPI collective worker %d-%d starting' % (worker_id, rank))
        logger.info('MPI collective worker %d-%d args: %s' % (worker_id, rank, str(my_args)))
        worker = MPICollectiveWorker(comm, worker_id)
        fun = None
        if len(my_args) > 3:
            fun_name = my_args[3]
            module = my_args[4]
            if module not in sys.modules:
                importlib.import_module(module)
            fun = eval(fun_name, sys.modules[module].__dict__)
        if fun is not None:
            parent_comm = MPI.Comm.Get_parent()
            args = parent_comm.bcast(None, root=0)
            fun(worker, *args)
        worker.serve()
    
