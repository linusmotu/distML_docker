from zmq.eventloop.ioloop import IOLoop
from zmq.eventloop.zmqstream import ZMQStream
from .constants import NBR_WORKERS, NBR_CLIENTS
import time
import json
from .taskcreator import TaskCreator
from multiprocessing import Process, Queue
import queue

# https://github.com/zeromq/pyzmq/issues/1091

current_milli_time = lambda: int(round(time.time() * 1000))
HEARTBEAT_LIVENESS = 3     # 3..5 is reasonable
HEARTBEAT_INTERVAL = 1.0   # Seconds

class Worker(object):
    def __init__(self, address):
        self.address = address
        self.expiry = time.time() + HEARTBEAT_INTERVAL * HEARTBEAT_LIVENESS
    pass

class LRUQueue(object):
    """LRUQueue class using ZMQStream/IOLoop for event dispatching"""

    def __init__(self, backend_socket, frontend_socket, heartbeat_socket):
        self.available_workers = 0
        self.unique_workers = []
        self.workers = []
        self.task_queue = []
        self.task_count = 0
        self.worker_stat = {}

        self.client_nbr = NBR_CLIENTS

        self.backend = ZMQStream(backend_socket)
        self.frontend = ZMQStream(frontend_socket)
        self.heartbeat = ZMQStream(heartbeat_socket)

        self.backend.on_recv(self.handle_backend)
        self.heartbeat.on_recv(self.handle_heartbeat)

        self.loop = IOLoop.instance()
        self.mpq = Queue()
        
        self.worker_status = {}

    def readyWorker(self, worker):
        """Prepare a worker by popping in the queue and placing inside again"""
        pass

    def purge(self):
        """Look for & kill expired workers."""
        pass

    # TODO: need to handle heartbeats itself, if the time is not new
    # for N seconds, remove it from the available worker list
    # Just time probably.
    def handle_heartbeat(self, msg):
        # print("Received heartbeat: ", msg)
        worker_id, _, _, status, last_beat = msg
        curr_stat = {}
        curr_stat['last_beat'] = last_beat
        curr_stat['status'] = status
        
        self.worker_status[worker_id] = curr_stat

        self.heartbeat.send_multipart([msg[0],
                                b'',
                                b"Pong..."])

        print("Curr time is: {}".format(str(int(time.time()))))
        print(self.worker_status)

        # Is this the best place to put this?
        self.purge()

    def handle_backend(self, msg):
        # Queue worker address for LRU routing
        worker_addr, empty, client_addr = msg[:3]

        assert self.available_workers < NBR_WORKERS

        # add worker back to the list of workers
        self.available_workers += 1
        print('Added to available workers (new total): ', self.available_workers)
        # self.workers.append(worker_addr)
        self.workers.insert(0, worker_addr)
        self.unique_workers.append(worker_addr)

        #   Second frame is empty
        assert empty == b""

        # Third frame is READY or else a client reply address
        if client_addr == b'READY':
            print("Received READY signal from {} BE".format(worker_addr.decode("utf-8")))

        # If client reply, send rest back to frontend
        if client_addr != b"READY":
            empty, reply = msg[3:]
            
            print("some reply: {}".format(reply.decode('utf-8')))
            # Following frame is empty
            assert empty == b""

            print("Received task, done by {}, to be sent to {}".format(
                worker_addr.decode("utf-8"), 
                client_addr.decode("utf-8")))
            print("Checking task list which has {} tasks left".format(len(self.task_queue)))
            # Check if still some task is available
            if self.task_queue:
                task = self.task_queue.pop()
                print("Number of workers: {}".format(len(self.workers)))
                worker_id = self.workers.pop()
                self.available_workers -= 1
                print("Sending to {}".format(worker_id))
                if worker_id.decode('utf-8') in self.worker_stat:
                    self.worker_stat[worker_id.decode('utf-8')] = int(self.worker_stat[worker_id.decode('utf-8')]) + 1
                else:
                    self.worker_stat[worker_id.decode('utf-8')] = 1
                print("Stat of worker:", self.worker_stat[worker_id.decode("utf-8")])

                self.backend.send_multipart([worker_id,
                                                b'',
                                                client_addr,
                                                b'',
                                                task.encode('ascii')])
                self.task_count += 1
            else:
                print("No more tasks available. Sent a total of {} tasks to {} unique workers".format(
                    str(self.task_count), len(set(self.unique_workers))
                ))

                print("stats:", self.worker_stat)
                # Receive something from backend and if the same length as the task queue
                # Aggregate and then send a response to the client
                # Only reply when broker is done collecting?
                self.frontend.send_multipart([client_addr, b'', reply])

        # Should change this? Or I should break down the data 
        # received on the frontend to multiple tasks for different workers.
        # a queue of tasks.
        if self.available_workers == 1:
            # on first recv, start accepting frontend messages
            self.frontend.on_recv(self.handle_frontend)
            # If a worker is available, pop a task from the queue.

    def distribute_tasks(self, client_addr):
        for _ in range(len(self.workers) - 1):
            self.available_workers -= 1
            task = self.task_queue.pop()
            worker_id = self.workers.pop()

            print("Sending to {}".format(worker_id))
            if worker_id.decode('utf-8') in self.worker_stat:
                self.worker_stat[worker_id.decode('utf-8')] = int(self.worker_stat[worker_id.decode('utf-8')]) + 1
            else:
                self.worker_stat[worker_id.decode('utf-8')] = 1
            self.task_count += 1
            self.backend.send_multipart([worker_id,
                                b'',
                                client_addr,
                                b'',
                                task.encode('ascii')])

    def handle_frontend(self, msg):
        print("Entered handle_frontend()...")
        # Now get next client request, route to LRU worker
        # Client request is [address][empty][request]
        client_addr, empty, request = msg
        print(client_addr)
        assert empty == b""

        tc = TaskCreator.TaskCreator(request)
        self.task_queue = tc.parse_request()
        
        print("Task Queue:", self.task_queue)

        self.distribute_tasks(client_addr)

        if self.available_workers == 0:
            print('Stopping reception until workers are available.')
            # stop receiving until workers become available again
            self.frontend.stop_on_recv()