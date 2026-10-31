"""Process-group termination for ServerSupervisor (ADR-034)."""

import os
import signal
import subprocess
import time
import unittest

from agent.adapters.llama_cpp import ServerSupervisor


def alive(pid: int) -> bool:
    """Return True only for a live (non-zombie) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            state = handle.read().split()[2]
        return state != "Z"
    except FileNotFoundError:
        return False


class ProcessTreeTerminationTest(unittest.TestCase):
    def test_terminates_group_and_is_idempotent(self) -> None:
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 60 & echo $!; wait"],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = -1
        pgid = -1
        try:
            assert proc.stdout is not None
            child_pid = int(proc.stdout.readline().strip())
            proc.stdout.close()
            time.sleep(0.2)
            pgid = os.getpgid(proc.pid)

            self.assertNotEqual(pgid, os.getpgrp())
            self.assertTrue(alive(child_pid), "grandchild should be alive")

            ServerSupervisor._terminate_process_tree(proc, grace_s=2.0)
            proc.wait(timeout=5)
            time.sleep(0.5)

            self.assertFalse(alive(proc.pid), "server process survived")
            self.assertFalse(alive(child_pid), "grandchild survived")

            # Idempotent: second call on a dead process must not raise.
            ServerSupervisor._terminate_process_tree(proc, grace_s=0.5)
        finally:
            if proc.poll() is None:
                try:
                    if pgid not in (-1, os.getpgrp()):
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
            if child_pid > 0 and alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
