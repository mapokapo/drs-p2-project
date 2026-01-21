"""Distributed node with Ricart-Agrawala mutex and bully election."""

import argparse
import json
import os
import random
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

from cloudwatch_logger import CloudWatchLogger

HEARTBEAT_INTERVAL = 2.0
ELECTION_TIMEOUT = 5.0
MUTEX_REPLY_TIMEOUT = 5.0


def send_bytes(sock: socket.socket, data: bytes) -> None:
    length_prefix = struct.pack(">I", len(data))
    sock.sendall(length_prefix + data)


def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    data = b""
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                return None
            data += packet
        except socket.error:
            return None
    return data


class MessageType(Enum):
    """Protocol message kinds exchanged between nodes."""

    REQUEST = "REQUEST"
    REPLY = "REPLY"
    ELECTION = "ELECTION"
    ANSWER = "ANSWER"
    COORDINATOR = "COORDINATOR"
    HEARTBEAT = "HEARTBEAT"


class NodeState(Enum):
    """Mutex state used by Ricart-Agrawala mutual exclusion."""

    RELEASED = "RELEASED"
    WANTED = "WANTED"
    HELD = "HELD"


class PeerAddress(NamedTuple):
    ip: str
    port: int


@dataclass
class ElectionState:
    coordinator_id: Optional[int] = None
    in_progress: bool = False
    received_answer: bool = False
    last_heartbeat: float = field(default_factory=time.time)


class ThreadSafeSet:
    """Thread-safe set for tracking nodes."""

    def __init__(self) -> None:
        self._set: set[int] = set()
        self._lock: threading.Lock = threading.Lock()

    def add(self, item: int) -> None:
        with self._lock:
            self._set.add(item)

    def discard(self, item: int) -> None:
        with self._lock:
            self._set.discard(item)

    def __contains__(self, item: int) -> bool:
        with self._lock:
            return item in self._set

    def __len__(self) -> int:
        with self._lock:
            return len(self._set)

    def snapshot(self) -> set[int]:
        with self._lock:
            return set(self._set)


class DistributedNode:
    """Cluster node with TCP messaging, election, and mutex coordination.

    Lock hierarchy (outer to inner): conn_lock -> dead_nodes lock -> mutex_lock
    -> clock_lock. Acquire in this order when multiple locks are needed to
    avoid deadlocks.
    """

    def __init__(
        self, node_id: int, peers: Dict[int, PeerAddress], local_port: int
    ) -> None:
        self.node_id: int = node_id
        self.peers: Dict[int, PeerAddress] = peers
        self.port: int = local_port

        self.lamport_clock: int = 0
        self.clock_lock: threading.Lock = threading.Lock()

        self.state: NodeState = NodeState.RELEASED
        self.deferred_replies: list[int] = []
        self.replies_received: set[int] = set()
        self.request_clock: int = 0
        self.mutex_lock: threading.Lock = threading.Lock()
        self.received_replies_event: threading.Event = threading.Event()

        self.election_state: ElectionState = ElectionState()

        self.peer_connections: Dict[int, socket.socket] = {}
        self.conn_lock: threading.Lock = threading.Lock()
        self.dead_nodes: ThreadSafeSet = ThreadSafeSet()
        self.shared_counter: int = 0

        self.server_socket: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("", self.port))
        self.server_socket.listen(5)

        # Initialize CloudWatch logger (checks USE_CLOUDWATCH env var internally)
        use_cloudwatch = os.environ.get("USE_CLOUDWATCH", "False").lower() == "true"
        self.cw_logger = CloudWatchLogger(node_id=self.node_id, enabled=use_cloudwatch)

        self.running: bool = True

    # --- Lamport Clock ---
    def tick(self) -> int:
        with self.clock_lock:
            self.lamport_clock += 1
        return self.lamport_clock

    def update_clock(self, received_time: int) -> None:
        with self.clock_lock:
            self.lamport_clock = max(self.lamport_clock, received_time) + 1

    def _expected_replies(self) -> int:
        return max(0, len(self.peers) - 1 - len(self.dead_nodes))

    # --- Connections & Messaging ---
    def _get_connection(self, target_id: int) -> Optional[socket.socket]:
        """Open or reuse a TCP connection to a peer."""
        with self.conn_lock:
            if target_id in self.peer_connections:
                return self.peer_connections[target_id]

            if target_id not in self.peers:
                return None

            peer_address = self.peers[target_id]
            target_ip = peer_address.ip
            target_port = peer_address.port
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect((target_ip, target_port))
                s.settimeout(None)
                self.peer_connections[target_id] = s
                self.dead_nodes.discard(target_id)
                return s
            except Exception as e:
                if target_id not in self.dead_nodes:
                    self.cw_logger.log_event(
                        "CONNECTION_ERROR",
                        f"Failed to connect to Node {target_id}",
                        self.lamport_clock,
                        error=str(e),
                    )
                return None

    def _try_send(self, target_id: int, json_data: bytes) -> bool:
        sock = self._get_connection(target_id)
        if not sock:
            return False
        try:
            send_bytes(sock, json_data)
            return True
        except (BrokenPipeError, ConnectionResetError, socket.error):
            with self.conn_lock:
                if target_id in self.peer_connections:
                    try:
                        self.peer_connections[target_id].close()
                    except Exception:
                        pass
                    del self.peer_connections[target_id]
            return False

    def send_message(
        self, target_id: int, msg_type: MessageType, **kwargs: Any
    ) -> None:
        if target_id not in self.peers:
            return

        if msg_type == MessageType.HEARTBEAT and target_id in self.dead_nodes:
            return

        msg: Dict[str, Any] = {
            "sender": self.node_id,
            "type": msg_type.value,
            "timestamp": self.tick(),
            **kwargs,
        }
        json_data = json.dumps(msg).encode("utf-8")

        for _ in range(2):
            if self._try_send(target_id, json_data):
                return

        if target_id not in self.dead_nodes:
            self.dead_nodes.add(target_id)
            self.cw_logger.log_event(
                "NODE_DOWN",
                f"Failed to send message to {target_id}. Marking as dead.",
                self.lamport_clock,
            )
            with self.mutex_lock:
                if self.state == NodeState.WANTED:
                    self._maybe_signal_replies_complete()

    def handle_client_connection(
        self, client_sock: socket.socket, addr: Tuple[str, int]
    ) -> None:
        client_sock.settimeout(None)
        try:
            while self.running:
                len_bytes = recv_exact(client_sock, 4)
                if not len_bytes:
                    break
                msg_len = struct.unpack(">I", len_bytes)[0]
                msg_bytes = recv_exact(client_sock, msg_len)
                if not msg_bytes:
                    break
                msg = json.loads(msg_bytes.decode("utf-8"))
                self.process_message(msg)
        except Exception:
            pass
        finally:
            client_sock.close()

    def listen(self) -> None:
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                threading.Thread(
                    target=self.handle_client_connection,
                    args=(client, addr),
                    daemon=True,
                ).start()
            except OSError:
                break
            except Exception as e:
                self.cw_logger.log_event("LISTENER_ERROR", str(e), self.lamport_clock)

    def process_message(self, msg: Dict[str, Any]) -> None:
        sender: int = msg["sender"]
        msg_type: MessageType = MessageType(msg["type"])
        msg_time: int = msg["timestamp"]

        self.dead_nodes.discard(sender)
        self.update_clock(msg_time)

        handlers: Dict[MessageType, Callable[[int, int], None]] = {
            MessageType.REQUEST: self.handle_request,
            MessageType.REPLY: self.handle_reply,
            MessageType.ELECTION: self.handle_election,
            MessageType.COORDINATOR: self.handle_coordinator,
            MessageType.ANSWER: self.handle_answer,
            MessageType.HEARTBEAT: self.handle_heartbeat,
        }
        handlers[msg_type](sender, msg_time)

    # --- Ricart-Agrawala Mutex ---
    def request_critical_section(self) -> None:
        """Request entry to the critical section using Ricart-Agrawala."""
        with self.mutex_lock:
            if self.state != NodeState.RELEASED:
                return
            self.state = NodeState.WANTED
            self.request_clock = self.tick()
            self.replies_received.clear()
            self.received_replies_event.clear()
            expected_replies = self._expected_replies()

        self.cw_logger.log_event(
            "MUTEX",
            "Requesting Critical Section",
            self.lamport_clock,
            req_clock=self.request_clock,
        )

        if expected_replies == 0:
            self.enter_critical_section()
            return

        dead_snapshot = self.dead_nodes.snapshot()
        for peer_id in self.peers:
            if peer_id != self.node_id and peer_id not in dead_snapshot:
                self.send_message(peer_id, MessageType.REQUEST)

        if self.received_replies_event.wait(timeout=MUTEX_REPLY_TIMEOUT):
            self.enter_critical_section()
            return

        with self.mutex_lock:
            dead_snapshot = self.dead_nodes.snapshot()
            missing_peers = {
                pid
                for pid in self.peers
                if pid != self.node_id
                and pid not in self.replies_received
                and pid not in dead_snapshot
            }
            if missing_peers:
                for pid in missing_peers:
                    self.dead_nodes.add(pid)
                self._maybe_signal_replies_complete()

        if self.received_replies_event.is_set():
            self.enter_critical_section()
        else:
            self.cw_logger.log_event(
                "MUTEX_FAIL",
                "Timeout waiting for replies. Releasing.",
                self.lamport_clock,
            )
            with self.mutex_lock:
                self.state = NodeState.RELEASED

    def handle_request(self, sender: int, sender_clock: int) -> None:
        """Queue or grant a mutex reply based on Lamport ordering."""
        reply: bool = False
        with self.mutex_lock:
            my_priority_higher = (self.state == NodeState.HELD) or (
                self.state == NodeState.WANTED
                and (
                    self.request_clock < sender_clock
                    or (self.request_clock == sender_clock and self.node_id < sender)
                )
            )

            if my_priority_higher:
                self.deferred_replies.append(sender)
            else:
                reply = True

        if reply:
            self.send_message(sender, MessageType.REPLY)

    def handle_reply(self, sender: int, _msg_time: Optional[int] = None) -> None:
        with self.mutex_lock:
            self.replies_received.add(sender)
            self._maybe_signal_replies_complete()

    def enter_critical_section(self) -> None:
        """Simulated critical section workload."""
        with self.mutex_lock:
            self.state = NodeState.HELD

        before_value = self.shared_counter
        self.cw_logger.log_event(
            "CS_RESOURCE",
            "Shared counter before increment",
            self.lamport_clock,
            counter=before_value,
        )
        self.shared_counter = before_value + 1
        after_value = self.shared_counter
        self.cw_logger.log_event(
            "CS_RESOURCE",
            "Shared counter after increment",
            self.lamport_clock,
            counter=after_value,
        )

        self.cw_logger.log_event(
            "CS_ENTER", ">>> ENTERING CRITICAL SECTION <<<", self.lamport_clock
        )
        
        print(f"Node {self.node_id} working in critical section...")
        for i in range(3):
             time.sleep(1.0)
             print(f"Node {self.node_id} performing exclusive task {i+1}/3...")

        self.cw_logger.log_event(
            "CS_EXIT", "<<< EXITING CRITICAL SECTION >>>", self.lamport_clock
        )
        self.exit_critical_section()

    def exit_critical_section(self) -> None:
        with self.mutex_lock:
            self.state = NodeState.RELEASED
            for deferred_node in self.deferred_replies:
                self.send_message(deferred_node, MessageType.REPLY)
            self.deferred_replies.clear()

    # --- Bully Election ---
    def start_election(self) -> None:
        """Run the bully election protocol to select a coordinator."""
        if self.election_state.in_progress:
            return
        time.sleep(random.uniform(0.1, 0.5))
        self.election_state.in_progress = True
        self.election_state.received_answer = False
        self.cw_logger.log_event(
            "ELECTION_START", "Starting Election Process", self.tick()
        )

        dead_snapshot = self.dead_nodes.snapshot()
        higher_nodes = [
            pid for pid in self.peers if pid > self.node_id and pid not in dead_snapshot
        ]

        if not higher_nodes:
            self.become_coordinator()
        else:
            for pid in higher_nodes:
                self.send_message(pid, MessageType.ELECTION)
            threading.Thread(target=self._wait_for_election_result, daemon=True).start()

    def _wait_for_election_result(self) -> None:
        time.sleep(ELECTION_TIMEOUT)
        if not self.election_state.in_progress:
            return

        if self.election_state.received_answer:
            time.sleep(ELECTION_TIMEOUT)
            if self.election_state.in_progress:
                self.cw_logger.log_event(
                    "ELECTION_RESTART",
                    "Timeout waiting for coordinator. Restarting.",
                    self.lamport_clock,
                )
                self.election_state.in_progress = False
                self.start_election()
        else:
            self.become_coordinator()

    def handle_election(self, sender: int, _msg_time: Optional[int] = None) -> None:
        if self.election_state.coordinator_id == self.node_id:
            self.send_message(sender, MessageType.COORDINATOR)
            return

        self.send_message(sender, MessageType.ANSWER)
        if not self.election_state.in_progress:
            self.start_election()

    def handle_answer(self, sender: int, _msg_time: Optional[int] = None) -> None:
        self.election_state.received_answer = True

    def handle_coordinator(self, sender: int, _msg_time: Optional[int] = None) -> None:
        self.election_state.in_progress = False
        self.election_state.last_heartbeat = time.time()

        if self.election_state.coordinator_id == sender:
            return

        self.election_state.coordinator_id = sender
        self.cw_logger.log_event(
            "LEADER_UPDATE", f"New Leader is Node {sender}", self.lamport_clock
        )

    def become_coordinator(self) -> None:
        self.election_state.coordinator_id = self.node_id
        self.election_state.in_progress = False
        self.cw_logger.log_event(
            "LEADER_SELF", "!!! I am the Coordinator !!!", self.tick()
        )
        for pid in self.peers:
            if pid != self.node_id:
                self.send_message(pid, MessageType.COORDINATOR)

    # --- Heartbeats & Liveness ---
    def handle_heartbeat(self, sender: int, _msg_time: Optional[int] = None) -> None:
        if self.election_state.coordinator_id == sender:
            self.election_state.last_heartbeat = time.time()
        elif self.election_state.coordinator_id is None:
            self.election_state.coordinator_id = sender
            self.cw_logger.log_event(
                "LEADER_RECOVER",
                f"Accepted Leader {sender} via Heartbeat",
                self.lamport_clock,
            )

    def run_heartbeat_loop(self) -> None:
        """Monitor and emit coordinator heartbeats with jitter."""
        while self.running:
            time.sleep(1.0 + random.uniform(0.0, 0.25))
            if self.election_state.coordinator_id == self.node_id:
                for pid in self.peers:
                    if pid != self.node_id:
                        self.send_message(pid, MessageType.HEARTBEAT)
            elif self.election_state.coordinator_id is not None:
                if time.time() - self.election_state.last_heartbeat > (
                    HEARTBEAT_INTERVAL + 4
                ):
                    self.cw_logger.log_event(
                        "LEADER_DEAD",
                        f"Leader {self.election_state.coordinator_id} timed out.",
                        self.lamport_clock,
                    )
                    self.dead_nodes.add(self.election_state.coordinator_id)
                    self.election_state.coordinator_id = None
                    self.start_election()

    # --- Shutdown & Lifecycle ---
    def shutdown(self) -> None:
        """Close sockets and stop background loops."""
        if not self.running:
            return
        self.running = False
        try:
            self.server_socket.close()
        except Exception:
            pass
        with self.conn_lock:
            for sock in self.peer_connections.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self.peer_connections.clear()
        self.cw_logger.log_event(
            "SYSTEM", "Node shutdown complete.", self.lamport_clock
        )

    def _maybe_signal_replies_complete(self) -> None:
        if len(self.replies_received) >= self._expected_replies():
            self.received_replies_event.set()


def parse_args() -> tuple[int, dict[int, PeerAddress]]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--peers", type=str, default="peers.json")
    args = parser.parse_args()

    try:
        with open(args.peers, "r") as f:
            config = json.load(f)
            peers_map = {
                int(k): PeerAddress(ip=v["ip"], port=v["port"])
                for k, v in config.items()
            }

            if args.id not in peers_map:
                print(f"Error: Node ID {args.id} not found in {args.peers}")
                sys.exit(1)

    except FileNotFoundError:
        print("Error: peers.json not found.")
        sys.exit(1)

    return args.id, peers_map


def run_repl(node: "DistributedNode") -> None:
    def _handle_shutdown(signum: int, frame: Optional[object]) -> None:
        """Handle SIGINT/SIGTERM with a clean shutdown."""
        node.cw_logger.log_event(
            "SYSTEM", f"Received signal {signum}, shutting down.", node.lamport_clock
        )
        node.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    try:
        while True:
            cmd = input().strip()
            if cmd == "req":
                threading.Thread(
                    target=node.request_critical_section, daemon=True
                ).start()
            elif cmd == "elect":
                node.start_election()
            elif cmd == "status":
                print(
                    f"Leader: {node.election_state.coordinator_id}, State: {node.state}"
                )
            elif cmd in {"quit", "kill", "exit"}:
                raise KeyboardInterrupt
            elif cmd == "help":
                print("Commands: req | elect | status | kill/quit/exit | help")
    except KeyboardInterrupt:
        node.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    node_id, peers_map = parse_args()
    my_port = peers_map[node_id].port

    node = DistributedNode(node_id, peers_map, my_port)

    threading.Thread(target=node.listen, daemon=True).start()
    threading.Thread(target=node.run_heartbeat_loop, daemon=True).start()

    node.cw_logger.log_event(
        "SYSTEM", f"Node {node.node_id} started.", node.tick()
    )

    time.sleep(2)

    if node.election_state.coordinator_id is None:
        threading.Thread(target=node.start_election, daemon=True).start()

    run_repl(node)
