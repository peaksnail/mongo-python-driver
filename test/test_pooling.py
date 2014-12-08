# Copyright 2009-2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test built in connection-pooling with threads."""

import gc
import random
import socket
import sys
import threading
import time

from pymongo import MongoClient
from pymongo.errors import (ConfigurationError,
                            ConnectionFailure,
                            DuplicateKeyError,
                            ExceededMaxWaiters)

sys.path[0:0] = [""]

from pymongo.pool import Pool, PoolOptions, _closed
from test import host, port, SkipTest, unittest, client_context
from test.utils import (get_pool,
                        joinall,
                        delay,
                        one,
                        rs_or_single_client)


@client_context.require_connection
def setUpModule():
    pass

N = 10
DB = "pymongo-pooling-tests"


def gc_collect_until_done(threads, timeout=60):
    start = time.time()
    running = list(threads)
    while running:
        assert (time.time() - start) < timeout, "Threads timed out"
        for t in running:
            t.join(0.1)
            if not t.isAlive():
                running.remove(t)
        gc.collect()


class MongoThread(threading.Thread):
    """A thread that uses a MongoClient."""
    def __init__(self, client):
        super(MongoThread, self).__init__()
        self.daemon = True  # Don't hang whole test if thread hangs.
        self.client = client
        self.db = self.client[DB]
        self.passed = False

    def run(self):
        self.run_mongo_thread()
        self.passed = True

    def run_mongo_thread(self):
        raise NotImplementedError


class SaveAndFind(MongoThread):
    def run_mongo_thread(self):
        for _ in range(N):
            rand = random.randint(0, N)
            _id = self.db.sf.save({"x": rand})
            assert rand == self.db.sf.find_one(_id)["x"]


class Unique(MongoThread):
    def run_mongo_thread(self):
        for _ in range(N):
            self.db.unique.insert({})  # no error


class NonUnique(MongoThread):
    def run_mongo_thread(self):
        for _ in range(N):
            try:
                self.db.unique.insert({"_id": "jesse"})
            except DuplicateKeyError:
                pass
            else:
                raise AssertionError("Should have raised DuplicateKeyError")


class Disconnect(MongoThread):
    def run_mongo_thread(self):
        for _ in range(N):
            self.client.disconnect()


class SocketGetter(MongoThread):
    """Utility for _TestMaxOpenSockets and _TestWaitQueueMultiple"""
    def __init__(self, client, pool):
        super(SocketGetter, self).__init__(client)
        self.state = 'init'
        self.pool = pool
        self.sock = None

    def run_mongo_thread(self):
        self.state = 'get_socket'

        # Pass 'checkout' so we can hold the socket.
        with self.pool.get_socket({}, 0, 0, checkout=True) as sock:
            self.sock = sock

        self.state = 'sock'

    def __del__(self):
        if self.sock:
            self.sock.close()


def run_cases(client, cases):
    threads = []
    n_runs = 5

    for case in cases:
        for i in range(n_runs):
            t = case(client)
            t.start()
            threads.append(t)

    for t in threads:
        t.join()

    for t in threads:
        assert t.passed, "%s.run() threw an exception" % repr(t)


class _TestPoolingBase(unittest.TestCase):
    """Base class for all connection-pool tests."""

    def setUp(self):
        self.c = rs_or_single_client()
        db = self.c[DB]
        db.unique.drop()
        db.test.drop()
        db.unique.insert({"_id": "jesse"})
        db.test.insert([{} for _ in range(10)])

    def create_pool(self, pair=(host, port), *args, **kwargs):
        return Pool(pair, PoolOptions(*args, **kwargs))


class TestPooling(_TestPoolingBase):
    def test_max_pool_size_validation(self):
        self.assertRaises(
            ConfigurationError, MongoClient, host=host, port=port,
            max_pool_size=-1)

        self.assertRaises(
            ConfigurationError, MongoClient, host=host, port=port,
            max_pool_size='foo')

        c = MongoClient(host=host, port=port, max_pool_size=100)
        self.assertEqual(c.max_pool_size, 100)

    def test_no_disconnect(self):
        run_cases(self.c, [NonUnique, Unique, SaveAndFind])

    def test_disconnect(self):
        run_cases(self.c, [SaveAndFind, Disconnect, Unique])

    def test_pool_reuses_open_socket(self):
        # Test Pool's _check_closed() method doesn't close a healthy socket.
        cx_pool = self.create_pool(max_pool_size=10)
        cx_pool._check_interval_seconds = 0  # Always check.
        with cx_pool.get_socket({}, 0, 0) as sock_info:
            pass

        with cx_pool.get_socket({}, 0, 0) as new_sock_info:
            self.assertEqual(sock_info, new_sock_info)

        self.assertEqual(1, len(cx_pool.sockets))

    def test_get_socket_and_exception(self):
        # get_socket() returns socket after a non-network error.
        cx_pool = self.create_pool(max_pool_size=1, wait_queue_timeout=1)
        with self.assertRaises(ZeroDivisionError):
            with cx_pool.get_socket({}, 0, 0) as sock_info:
                1 / 0

        # Socket was returned, not closed.
        with cx_pool.get_socket({}, 0, 0) as new_sock_info:
            self.assertEqual(sock_info, new_sock_info)

        self.assertEqual(1, len(cx_pool.sockets))

    def test_pool_removes_dead_socket(self):
        # Test that Pool removes dead socket and the socket doesn't return
        # itself PYTHON-344
        cx_pool = self.create_pool(max_pool_size=10)
        cx_pool._check_interval_seconds = 0  # Always check.

        with cx_pool.get_socket({}, 0, 0) as sock_info:
            # Simulate a closed socket without telling the SocketInfo it's
            # closed.
            sock_info.sock.close()
            self.assertTrue(_closed(sock_info.sock))

        with cx_pool.get_socket({}, 0, 0) as new_sock_info:
            self.assertEqual(0, len(cx_pool.sockets))
            self.assertNotEqual(sock_info, new_sock_info)

        self.assertEqual(1, len(cx_pool.sockets))

    def test_pool_with_fork(self):
        # Test that separate MongoClients have separate Pools, and that the
        # driver can create a new MongoClient after forking
        if sys.platform == "win32":
            raise SkipTest("Can't test forking on Windows")

        try:
            from multiprocessing import Process, Pipe
        except ImportError:
            raise SkipTest("No multiprocessing module")

        a = rs_or_single_client()
        a.pymongo_test.test.remove()
        a.pymongo_test.test.insert({'_id':1})
        a.pymongo_test.test.find_one()
        self.assertEqual(1, len(get_pool(a).sockets))
        a_sock = one(get_pool(a).sockets)

        def loop(pipe):
            c = rs_or_single_client()
            c.pymongo_test.test.find_one()
            self.assertEqual(1, len(get_pool(c).sockets))
            pipe.send(one(get_pool(c).sockets).sock.getsockname())

        cp1, cc1 = Pipe()
        cp2, cc2 = Pipe()

        p1 = Process(target=loop, args=(cc1,))
        p2 = Process(target=loop, args=(cc2,))

        p1.start()
        p2.start()

        p1.join(1)
        p2.join(1)

        p1.terminate()
        p2.terminate()

        p1.join()
        p2.join()

        cc1.close()
        cc2.close()

        b_sock = cp1.recv()
        c_sock = cp2.recv()
        self.assertTrue(a_sock.sock.getsockname() != b_sock)
        self.assertTrue(a_sock.sock.getsockname() != c_sock)
        self.assertTrue(b_sock != c_sock)

        # a_sock, created by parent process, is still in the pool
        with get_pool(a).get_socket({}, 0, 0) as d_sock:
            self.assertEqual(a_sock, d_sock)

    def test_wait_queue_timeout(self):
        wait_queue_timeout = 2  # Seconds
        pool = self.create_pool(
            max_pool_size=1, wait_queue_timeout=wait_queue_timeout)
        
        with pool.get_socket({}, 0, 0) as sock_info:
            start = time.time()
            with self.assertRaises(ConnectionFailure):
                with pool.get_socket({}, 0, 0):
                    pass

        duration = time.time() - start
        self.assertTrue(
            abs(wait_queue_timeout - duration) < 1,
            "Waited %.2f seconds for a socket, expected %f" % (
                duration, wait_queue_timeout))

        sock_info.close()

    def test_no_wait_queue_timeout(self):
        # Verify get_socket() with no wait_queue_timeout blocks forever.
        pool = self.create_pool(max_pool_size=1)
        
        # Reach max_size.
        with pool.get_socket({}, 0, 0) as s1:
            t = SocketGetter(self.c, pool)
            t.start()
            while t.state != 'get_socket':
                time.sleep(0.1)

            time.sleep(1)
            self.assertEqual(t.state, 'get_socket')

        while t.state != 'sock':
            time.sleep(0.1)

        self.assertEqual(t.state, 'sock')
        self.assertEqual(t.sock, s1)
        s1.close()

    def test_wait_queue_multiple(self):
        wait_queue_multiple = 3
        pool = self.create_pool(
            max_pool_size=2, wait_queue_multiple=wait_queue_multiple)

        # Reach max_size sockets.
        with pool.get_socket({}, 0, 0):
            with pool.get_socket({}, 0, 0):

                # Reach max_size * wait_queue_multiple waiters.
                threads = []
                for _ in range(6):
                    t = SocketGetter(self.c, pool)
                    t.start()
                    threads.append(t)

                time.sleep(1)
                for t in threads:
                    self.assertEqual(t.state, 'get_socket')

                with self.assertRaises(ExceededMaxWaiters):
                    with pool.get_socket({}, 0, 0):
                        pass

    def test_no_wait_queue_multiple(self):
        pool = self.create_pool(max_pool_size=2)

        socks = []
        for _ in range(2):
            # Pass 'checkout' so we can hold the socket.
            with pool.get_socket({}, 0, 0, checkout=True) as sock:
                socks.append(sock)

        threads = []
        for _ in range(30):
            t = SocketGetter(self.c, pool)
            t.start()
            threads.append(t)
        time.sleep(1)
        for t in threads:
            self.assertEqual(t.state, 'get_socket')

        for socket_info in socks:
            socket_info.close()


class TestPoolMaxSize(_TestPoolingBase):
    def test_max_pool_size(self):
        max_pool_size = 4
        c = rs_or_single_client(max_pool_size=max_pool_size)
        collection = c[DB].test

        # Need one document.
        collection.remove()
        collection.insert({})

        # nthreads had better be much larger than max_pool_size to ensure that
        # max_pool_size sockets are actually required at some point in this
        # test's execution.
        cx_pool = get_pool(c)
        nthreads = 10
        threads = []
        lock = threading.Lock()
        self.n_passed = 0

        def f():
            for _ in range(5):
                collection.find_one({'$where': delay(0.1)})
                assert len(cx_pool.sockets) <= max_pool_size

            with lock:
                self.n_passed += 1

        for i in range(nthreads):
            t = threading.Thread(target=f)
            threads.append(t)
            t.start()

        joinall(threads)
        self.assertEqual(nthreads, self.n_passed)
        self.assertTrue(len(cx_pool.sockets) > 1)
        self.assertEqual(max_pool_size, cx_pool._socket_semaphore.counter)

    def test_max_pool_size_none(self):
        c = rs_or_single_client(max_pool_size=None)
        collection = c[DB].test

        # Need one document.
        collection.remove()
        collection.insert({})

        cx_pool = get_pool(c)
        nthreads = 10
        threads = []
        lock = threading.Lock()
        self.n_passed = 0

        def f():
            for _ in range(5):
                collection.find_one({'$where': delay(0.1)})

            with lock:
                self.n_passed += 1

        for i in range(nthreads):
            t = threading.Thread(target=f)
            threads.append(t)
            t.start()

        joinall(threads)
        self.assertEqual(nthreads, self.n_passed)
        self.assertTrue(len(cx_pool.sockets) > 1)

    def test_max_pool_size_with_connection_failure(self):
        # The pool acquires its semaphore before attempting to connect; ensure
        # it releases the semaphore on connection failure.
        class TestPool(Pool):
            def connect(self):
                raise socket.error()

        test_pool = TestPool(
            ('example.com', 27017),
            PoolOptions(
                max_pool_size=1,
                connect_timeout=1,
                socket_timeout=1,
                wait_queue_timeout=1))

        # First call to get_socket fails; if pool doesn't release its semaphore
        # then the second call raises "ConnectionFailure: Timed out waiting for
        # socket from pool" instead of the socket.error.
        for i in range(2):
            with self.assertRaises(socket.error):
                with test_pool.get_socket({}, 0, 0, checkout=True):
                    pass


if __name__ == "__main__":
    unittest.main()
