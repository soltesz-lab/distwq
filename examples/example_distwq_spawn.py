# Example of using distributed work queue distwq
# PYTHONPATH must include the directories in which distwq and this file are located.

import pprint
import distwq
import numpy as np  
import scipy
from scipy import signal
from mpi4py import MPI

nprocs_per_worker = 3

def do_work(freq):
    rng = np.random.RandomState()
    fs = 10e3
    N = 1e5
    amp = 2*np.sqrt(2)
    freq = float(freq)
    noise_power = 0.001 * fs / 2
    time = np.arange(N) / fs
    x = amp*np.sin(2*np.pi*freq*time)
    x += rng.normal(scale=np.sqrt(noise_power), size=time.shape)
    f, pdens = signal.periodogram(x, fs)
    return f, pdens

def init(worker):
    if worker.worker_id == 1:
        req = worker.parent_comm.isend("inter send", dest=0)
        req.wait()
    else:
        req = worker.parent_comm.Ibarrier()
        data = worker.parent_comm.bcast(None, root=0)
        print("worker %d / rank %d: data = %s" % (worker.worker_id, worker.comm.rank, str(data)))
        req.wait()
    worker.comm.barrier()
        
def broker_init(broker):
    
    data = None
    if broker.worker_id == 1:
        status = MPI.Status()
        data = broker.sub_comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
        tag = status.Get_tag()

    if broker.worker_id == 1:
        broker.group_comm.bcast(data, root=0)
    else:
        data = broker.group_comm.bcast(None, root=0)
    broker.group_comm.barrier()
    print("broker %d: data = %s" % (broker.worker_id, str(data)))

    if broker.worker_id != 1:
        req = broker.sub_comm.Ibarrier()
        broker.sub_comm.bcast(data, root=MPI.ROOT)
        req.wait()
    broker.group_comm.barrier()
    
def main(controller):
    
    n = 5
    for i in range(0, n):
        controller.submit_call("do_work", (i+1,), module_name="example_distwq")
    s = []
    for i in range(0, n):
        s.append(controller.get_next_result())
    controller.info()
    pprint.pprint(s)

if __name__ == '__main__':
    if distwq.is_controller:
        distwq.run(fun_name="main", verbose=True, spawn_workers=True, nprocs_per_worker=nprocs_per_worker)
    else:
        distwq.run(fun_name="init", module_name="example_distwq",
                   broker_fun_name="broker_init", broker_module_name="example_distwq_spawn",
                   spawn_workers=True, nprocs_per_worker=nprocs_per_worker,
                   verbose=True)
