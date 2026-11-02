"""Human-side CLI: run the server, log in interactively, renew Kerberos, watch sessions, read the audit log.

    metacentrum-mcp serve            # stdio MCP server (what the harness launches)
    metacentrum-mcp login            # open the shared connection with password/OTP prompts if needed
    metacentrum-mcp kinit            # get/renew the Kerberos ticket on the frontend
    metacentrum-mcp status           # connection, Kerberos, workspace, sessions, jobs
    metacentrum-mcp attach <sid>     # watch (or with --rw take over) an agent's interactive terminal
    metacentrum-mcp audit [-n 50]    # recent tool calls
    metacentrum-mcp disconnect       # close the shared connection
    metacentrum-mcp selftest [-i]    # live end-to-end check against the cluster (tiny jobs)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .config import load_config
from .errors import McpGridError


def _gate(args):
    from .gate import Gate
    from .state import State

    cfg = load_config(args.config)
    return cfg, Gate(cfg, State(cfg.state_dir))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="metacentrum-mcp", description="MCP gateway to MetaCentrum PBS")
    ap.add_argument("--config", help="config TOML (default: $METACENTRUM_MCP_CONFIG or ~/.config/metacentrum-mcp/config.toml)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    lp = sub.add_parser("login", help="open the shared SSH connection interactively")
    lp.add_argument("--host")
    lp.add_argument("--user")
    sub.add_parser("kinit", help="obtain/renew a Kerberos ticket on the frontend (prompts for password)")
    sub.add_parser("status", help="show connection status")
    at = sub.add_parser("attach", help="attach to an interactive session's terminal")
    at.add_argument("session_id")
    at.add_argument("--rw", action="store_true", help="allow typing (default: read-only)")
    au = sub.add_parser("audit", help="show recent tool calls")
    au.add_argument("-n", type=int, default=30)
    sub.add_parser("disconnect", help="close the shared SSH connection")
    sub.add_parser("config", help="print the effective configuration")
    st = sub.add_parser("selftest", help="live end-to-end check against the cluster (tiny jobs)")
    st.add_argument("-i", "--interactive", action="store_true", help="also test an interactive job")
    st.add_argument("--timeout", type=int, default=20, help="minutes to wait for queued jobs")
    args = ap.parse_args(argv)

    if os.name == "nt":
        sys.exit("metacentrum-mcp needs a POSIX system (OpenSSH connection sharing). On Windows use WSL2; see README.")

    if args.cmd in (None, "serve"):
        from .server import main as serve

        serve(args.config)
        return

    try:
        if args.cmd == "login":
            cfg, gate = _gate(args)
            if args.host or args.user:
                from .ssh import SSHTransport

                host = args.host or gate.transport.host
                if host not in cfg.connection.allowed_hosts:
                    sys.exit(f"host {host} is not in allowed_hosts")
                gate.transport = SSHTransport(cfg, host, args.user or gate.transport.user)
                gate._target_file.write_text(json.dumps({"host": host, "user": gate.transport.user}))
            if not gate.transport.master_alive():
                print(f"Connecting to {gate.target} (you may be asked for a password/OTP)...")
                gate.transport.open_master(interactive=True)
            else:
                gate.transport.disconnect_flag.unlink(missing_ok=True)
            s = gate.summary(gate.probe())
            print(json.dumps(s, indent=2))
            if not s["kerberos"]["valid"]:
                print("\nNo Kerberos ticket on the frontend. Run: metacentrum-mcp kinit")
        elif args.cmd == "kinit":
            cfg, gate = _gate(args)
            user = gate.facts().get("user") or gate.transport.user
            rc = gate.transport.interactive_ssh(["kinit", f"{user}@META"])
            gate.probe()
            sys.exit(rc)
        elif args.cmd == "status":
            cfg, gate = _gate(args)
            alive = gate.transport.master_alive()
            out = {"target": gate.target, "connected": alive, "config": cfg.source,
                   "state_dir": str(cfg.state_dir)}
            if alive:
                out["master_pid"] = gate.transport.master_pid()
                out.update(gate.summary(gate.probe()))
                out["sessions"] = [
                    {k: s[k] for k in ("session_id", "job_id", "state", "pending_run")}
                    for s in gate.state.sessions(gate.target)
                ]
            fail = gate.transport.last_failure()
            if not alive and fail:
                out["last_login_failure"] = {**fail, "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(fail["at"]))}
                out["fix"] = "metacentrum-mcp login"
            out["registered_active_jobs"] = [
                {k: j[k] for k in ("job_id", "name", "last_state")} for j in gate.state.jobs(gate.target, active_only=True)
            ]
            print(json.dumps(out, indent=2))
        elif args.cmd == "attach":
            cfg, gate = _gate(args)
            s = gate.state.get_session(args.session_id)
            if not s:
                sys.exit(f"unknown session {args.session_id}")
            cmd = ["tmux", "-L", "mcp", "attach", "-t", s["tmux_name"]]
            if not args.rw:
                cmd.append("-r")
            print("Detach with Ctrl-b d." + ("" if args.rw else " Read-only; use --rw to type."), file=sys.stderr)
            sys.exit(gate.transport.interactive_ssh(cmd))
        elif args.cmd == "audit":
            cfg, gate = _gate(args)
            for e in gate.state.read_audit(args.n):
                status = "ok " if e.get("ok") else "ERR"
                extra = e.get("result") or e.get("error", "")
                print(f"{e['ts']} {status} {e['tool']:<20} {e.get('ms', '?'):>6}ms {json.dumps(e.get('args'))[:120]} {extra}")
        elif args.cmd == "disconnect":
            cfg, gate = _gate(args)
            print("closed" if gate.disconnect() else "no connection was open")
        elif args.cmd == "selftest":
            from .selftest import main as selftest

            sys.exit(selftest(args.config, args.interactive, args.timeout))
        elif args.cmd == "config":
            from dataclasses import asdict

            cfg = load_config(args.config)
            print(json.dumps(asdict(cfg), indent=2, default=str))
    except McpGridError as e:
        sys.exit(f"error: {e}")
