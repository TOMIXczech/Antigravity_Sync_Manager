#!/usr/bin/env python3
"""
Antigravity & Git Sync Studio
=============================
A standalone utility to view your Git repositories, copy or commit app changes,
and seamlessly pack/restore specific Antigravity conversations (including SQLite
trajectory databases, annotations, brain artifacts, and workspace path remapping)
so you can switch devices as if you never left.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


# ---------------------------------------------------------------------------
# Constants & Paths
# ---------------------------------------------------------------------------

DEFAULT_AGY_DIR = Path.home() / ".gemini" / "antigravity"
CONFIG_FILE = Path.home() / ".agy_git_sync_config.json"
REPO_CHATS_DIRNAME = ".antigravity_chats"
SYNC_META_DIRNAME = ".agy_sync_meta"


# ---------------------------------------------------------------------------
# Protobuf Wire-Format Helpers (for Workspace URI Remapping & Summary Proto)
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    res = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Unexpected EOF in varint")
        b = buf[pos]
        pos += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            return res, pos
        shift += 7


def _encode_varint(val: int) -> bytes:
    if val < 0:
        val += 1 << 64
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def rewrite_proto_bytes(data: bytes, replacements: list[tuple[bytes, bytes]]) -> bytes:
    """
    Recursively parses a protobuf message and replaces byte sequences inside
    length-delimited strings while accurately recalculating all parent varint lengths.
    """
    if not data or not replacements:
        return data
    if not any(old in data for old, _ in replacements):
        return data

    pos = 0
    n = len(data)
    out = bytearray()
    try:
        while pos < n:
            tag_val, next_pos = _read_varint(data, pos)
            wire_type = tag_val & 0x7
            field_num = tag_val >> 3
            if field_num == 0 or wire_type not in (0, 1, 2, 5):
                raise ValueError("Invalid wire format")
            pos = next_pos

            if wire_type == 0:
                val, pos = _read_varint(data, pos)
                out.extend(_encode_varint(tag_val))
                out.extend(_encode_varint(val))
            elif wire_type == 1:
                if pos + 8 > n:
                    raise ValueError("EOF in fixed64")
                out.extend(_encode_varint(tag_val))
                out.extend(data[pos : pos + 8])
                pos += 8
            elif wire_type == 5:
                if pos + 4 > n:
                    raise ValueError("EOF in fixed32")
                out.extend(_encode_varint(tag_val))
                out.extend(data[pos : pos + 4])
                pos += 4
            elif wire_type == 2:
                length, pos = _read_varint(data, pos)
                if pos + length > n:
                    raise ValueError("EOF in length-delimited field")
                sub = data[pos : pos + length]
                pos += length

                if any(old in sub for old, _ in replacements):
                    if sub.startswith(b"file://") or sub.startswith(b'["file://'):
                        for old, new in replacements:
                            sub = sub.replace(old, new)
                    else:
                        sub = rewrite_proto_bytes(sub, replacements)

                out.extend(_encode_varint(tag_val))
                out.extend(_encode_varint(len(sub)))
                out.extend(sub)
        return bytes(out)
    except Exception:
        res = data
        for old, new in replacements:
            res = res.replace(old, new)
        return res


def upsert_agyhub_summaries_proto(pb_path: Path, cid: str, raw_summary: bytes) -> None:
    """
    Updates or inserts a (conversation_id, raw_summary) map entry inside
    ~/.gemini/antigravity/agyhub_summaries_proto.pb.
    """
    if not raw_summary:
        return

    entries: list[tuple[str, bytes]] = []
    if pb_path.exists():
        try:
            data = pb_path.read_bytes()
            pos = 0
            while pos < len(data):
                tag, pos = _read_varint(data, pos)
                length, pos = _read_varint(data, pos)
                sub = data[pos : pos + length]
                pos += length
                if tag == 10:
                    _, p1 = _read_varint(sub, 0)
                    l1, p1 = _read_varint(sub, p1)
                    entry_cid = sub[p1 : p1 + l1].decode("utf-8", errors="replace")
                    _, p2 = _read_varint(sub, p1 + l1)
                    l2, p2 = _read_varint(sub, p2)
                    entry_raw = sub[p2 : p2 + l2]
                    entries.append((entry_cid, entry_raw))
        except Exception:
            pass

    updated = False
    new_entries: list[tuple[str, bytes]] = []
    for entry_cid, entry_raw in entries:
        if entry_cid == cid:
            new_entries.append((cid, raw_summary))
            updated = True
        else:
            new_entries.append((entry_cid, entry_raw))

    if not updated:
        new_entries.append((cid, raw_summary))

    out = bytearray()
    for entry_cid, entry_raw in new_entries:
        cid_bytes = entry_cid.encode("utf-8")
        sub = bytearray()
        sub.extend(_encode_varint((1 << 3) | 2))
        sub.extend(_encode_varint(len(cid_bytes)))
        sub.extend(cid_bytes)
        sub.extend(_encode_varint((2 << 3) | 2))
        sub.extend(_encode_varint(len(entry_raw)))
        sub.extend(entry_raw)

        out.extend(_encode_varint((1 << 3) | 2))
        out.extend(_encode_varint(len(sub)))
        out.extend(sub)

    pb_path.write_bytes(bytes(out))


# ---------------------------------------------------------------------------
# Path & URI Normalization
# ---------------------------------------------------------------------------

def path_to_agy_uris(folder_path: str | Path) -> tuple[str, str]:
    """
    Converts a local Windows/POSIX path into the two URI formats used by Antigravity:
    1. Encoded colon format (used in workspace_uris JSON & proto tag 7):
       file:///c%3A/Users/ufo/Downloads/TP
    2. Unencoded colon format (used in proto tag 1/2):
       file:///c:/Users/ufo/Downloads/TP
    """
    resolved = Path(folder_path).resolve()
    posix_str = resolved.as_posix()
    # Lowercase drive letter if Windows drive
    if len(posix_str) >= 2 and posix_str[1] == ":":
        drive = posix_str[0].lower()
        rest = posix_str[2:]
        rest_quoted = urllib.parse.quote(rest, safe="/")
        uri_plain = f"file:///{drive}:{rest_quoted}"
        uri_encoded = f"file:///{drive}%3A{rest_quoted}"
        return uri_encoded, uri_plain
    else:
        quoted = urllib.parse.quote(posix_str, safe="/")
        uri = f"file://{quoted}"
        return uri, uri


def agy_uri_to_path(uri: str) -> str:
    """Converts file:///c%3A/Users/... back to a readable OS path."""
    if not uri:
        return ""
    u = urllib.parse.unquote(uri)
    if u.startswith("file:///"):
        u = u[len("file:///") :]
    elif u.startswith("file://"):
        u = u[len("file://") :]
    return os.path.normpath(u)


def extract_first_workspace_uri(workspace_uris_json: str) -> str:
    try:
        arr = json.loads(workspace_uris_json)
        if isinstance(arr, list) and arr:
            return str(arr[0])
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Core Engine: Git + Antigravity Sync
# ---------------------------------------------------------------------------

class SyncEngine:
    def __init__(self, agy_dir: Path = DEFAULT_AGY_DIR, logger: Callable[[str], None] | None = None):
        self.agy_dir = agy_dir
        self.logger = logger or (lambda msg: print(msg))

    def log(self, msg: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.logger(f"[{timestamp}] {msg}")

    # ---- Config & Repo Discovery ----

    def load_config(self) -> dict[str, Any]:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"repos": [], "last_repo": ""}

    def save_config(self, cfg: dict[str, Any]) -> None:
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Warning: Could not save config: {e}")

    def discover_git_repos(self) -> list[str]:
        """Finds Git repositories from config, current directory, Antigravity DB, and common folders."""
        cfg = self.load_config()
        candidates: set[str] = set()

        # 1. Saved repos in config
        for r in cfg.get("repos", []):
            if os.path.isdir(r):
                candidates.add(os.path.normpath(r))

        # 2. Script directory & current working directory
        for base in [Path(__file__).resolve().parent, Path.cwd()]:
            for p in [base, *base.parents]:
                if (p / ".git").exists():
                    candidates.add(os.path.normpath(str(p)))
                    break

        # 3. Workspaces referenced in Antigravity conversation_summaries.db
        summaries_db = self.agy_dir / "conversation_summaries.db"
        if summaries_db.exists():
            try:
                conn = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True)
                for (ws_json,) in conn.execute("SELECT workspace_uris FROM conversation_summaries"):
                    uri = extract_first_workspace_uri(ws_json)
                    if uri:
                        p_str = agy_uri_to_path(uri)
                        if os.path.isdir(p_str):
                            candidates.add(os.path.normpath(p_str))
                conn.close()
            except Exception:
                pass

        # 4. Shallow scan of common user directories
        home = Path.home()
        search_roots = [
            home / "Downloads",
            home / "source" / "repos",
            home / "Documents",
            home / "Desktop",
            home / "Projects",
            home,
        ]
        for root in search_roots:
            if not root.is_dir():
                continue
            try:
                if (root / ".git").exists():
                    candidates.add(os.path.normpath(str(root)))
                for child in root.iterdir():
                    if child.is_dir() and not child.name.startswith("."):
                        if (child / ".git").exists():
                            candidates.add(os.path.normpath(str(child)))
            except Exception:
                pass

        valid_map: dict[str, str] = {}
        for c in candidates:
            if os.path.isdir(c):
                try:
                    resolved_str = str(Path(c).resolve())
                except Exception:
                    resolved_str = os.path.normpath(c)
                # Normalize Windows drive letter to uppercase
                if len(resolved_str) >= 2 and resolved_str[1] == ":":
                    resolved_str = resolved_str[0].upper() + resolved_str[1:]
                key = os.path.normcase(resolved_str)
                # Keep repositories that have .git (or are explicitly configured)
                if (Path(resolved_str) / ".git").exists() or c in cfg.get("repos", []):
                    valid_map[key] = resolved_str

        valid_repos = sorted(valid_map.values(), key=lambda s: s.lower())
        cfg["repos"] = valid_repos
        self.save_config(cfg)
        return valid_repos

    # ---- Git Operations ----

    def run_git(self, repo_path: str, args: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except Exception as e:
            return 1, "", str(e)

    def get_repo_status(self, repo_path: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "path": repo_path,
            "is_git": False,
            "branch": "N/A",
            "remote": "No remote configured",
            "ahead_behind": "",
            "changes": [],
            "recent_commits": [],
        }
        if not os.path.isdir(repo_path):
            return info

        code, out, _ = self.run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out != "true":
            return info

        info["is_git"] = True

        # Branch & tracking info
        _, branch_out, _ = self.run_git(repo_path, ["status", "-sb"])
        if branch_out:
            first_line = branch_out.splitlines()[0]
            info["branch"] = first_line.replace("## ", "").strip()

        # Remote URL
        _, rem_out, _ = self.run_git(repo_path, ["remote", "-v"])
        if rem_out:
            for line in rem_out.splitlines():
                if "(push)" in line or "(fetch)" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        info["remote"] = parts[1]
                        break

        # Porcelain status
        _, status_out, _ = self.run_git(repo_path, ["status", "--porcelain"])
        if status_out:
            for line in status_out.splitlines():
                if len(line) >= 3:
                    st = line[:2].strip() or "?"
                    filepath = line[3:].strip()
                    info["changes"].append((st, filepath))

        # Recent commits
        _, log_out, _ = self.run_git(repo_path, ["log", "-n", "5", "--oneline"])
        if log_out:
            info["recent_commits"] = log_out.splitlines()

        return info

    def git_commit_and_push(
        self,
        repo_path: str,
        message: str,
        push: bool = True,
        selected_files: list[str] | None = None,
    ) -> bool:
        if not message.strip():
            message = f"chore: sync app state & Antigravity chat ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})"

        if selected_files:
            self.log(f"Staging {len(selected_files)} selected item(s)...")
            code, _, err = self.run_git(repo_path, ["add", "--", *selected_files])
        else:
            self.log("Staging all repository changes (git add -A)...")
            code, _, err = self.run_git(repo_path, ["add", "-A"])

        if code != 0:
            self.log(f"Git add error: {err}")
            return False

        # Check if anything is staged
        _, diff_cached, _ = self.run_git(repo_path, ["diff", "--cached", "--name-only"])
        if not diff_cached.strip():
            self.log("No new file changes to commit (working tree is already clean).")
        else:
            self.log(f"Committing changes: '{message}'...")
            code, out, err = self.run_git(repo_path, ["commit", "-m", message])
            if code != 0:
                self.log(f"Git commit failed: {err or out}")
                return False
            self.log(f"Commit succeeded: {out.splitlines()[0] if out else 'OK'}")

        if push:
            self.log("Pushing commits to remote repository (git push)...")
            code, out, err = self.run_git(repo_path, ["push"])
            if code != 0:
                # Try pushing with upstream set to current branch
                _, branch, _ = self.run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
                branch = branch or "main"
                code, out, err = self.run_git(repo_path, ["push", "-u", "origin", branch])
                if code != 0:
                    self.log(f"Git push failed: {err or out}")
                    return False
            self.log("Successfully pushed to remote!")

        return True

    def git_pull(self, repo_path: str) -> bool:
        self.log(f"Pulling latest changes in {repo_path}...")
        code, out, err = self.run_git(repo_path, ["pull"])
        if code != 0:
            self.log(f"Git pull note/error: {err or out}")
            return False
        self.log(f"Git pull complete: {out or 'Already up to date.'}")
        return True

    def git_clone(self, url: str, target_dir: str) -> bool:
        self.log(f"Cloning {url} into {target_dir}...")
        try:
            proc = subprocess.run(
                ["git", "clone", url, target_dir],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if proc.returncode != 0:
                self.log(f"Clone failed: {proc.stderr or proc.stdout}")
                return False
            self.log("Clone finished successfully!")
            return True
        except Exception as e:
            self.log(f"Clone error: {e}")
            return False

    def copy_repo_changes_to_folder(self, repo_path: str, dest_folder: str, only_changed: bool = True) -> int:
        """Copies either modified/untracked files or the entire working tree (excluding .git) to dest_folder."""
        dest = Path(dest_folder)
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        src_root = Path(repo_path)

        if only_changed:
            status = self.get_repo_status(repo_path)
            for st, rel_path in status["changes"]:
                # git status quotes paths with spaces sometimes
                clean_rel = rel_path.strip('"')
                src_file = src_root / clean_rel
                if src_file.is_file():
                    target_file = dest / clean_rel
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, target_file)
                    copied += 1
        else:
            for item in src_root.rglob("*"):
                if ".git" in item.parts:
                    continue
                if item.is_file():
                    rel = item.relative_to(src_root)
                    target_file = dest / rel
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target_file)
                    copied += 1

        self.log(f"Copied {copied} file(s) from {repo_path} to {dest_folder}.")
        return copied

    # ---- Antigravity Conversations Inspection ----

    def _read_pbtxt_title(self, cid: str) -> str:
        ann_file = self.agy_dir / "annotations" / f"{cid}.pbtxt"
        if ann_file.exists():
            try:
                txt = ann_file.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'title:\s*"([^"]+)"', txt)
                if m:
                    return m.group(1)
            except Exception:
                pass
        return ""

    def list_local_conversations(self) -> list[dict[str, Any]]:
        """Lists all Antigravity conversations on this device."""
        convos: dict[str, dict[str, Any]] = {}
        subagent_ids: set[str] = set()

        # 1. Read from conversation_summaries.db
        summaries_db = self.agy_dir / "conversation_summaries.db"
        if summaries_db.exists():
            try:
                conn = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT * FROM conversation_summaries ORDER BY last_modified_time DESC"
                ):
                    cid = row["conversation_id"]
                    if row["parent_conversation_id"]:
                        subagent_ids.add(cid)
                        continue
                    ws_uri = extract_first_workspace_uri(row["workspace_uris"])
                    ws_path = agy_uri_to_path(ws_uri)
                    title = row["title"] or row["preview"] or self._read_pbtxt_title(cid) or f"Conversation {cid[:8]}"
                    convos[cid] = {
                        "id": cid,
                        "title": title,
                        "preview": row["preview"] or "",
                        "step_count": row["step_count"],
                        "last_modified": str(row["last_modified_time"])[:19],
                        "workspace_uri": ws_uri,
                        "workspace_path": ws_path,
                        "has_db": (self.agy_dir / "conversations" / f"{cid}.db").exists()
                        or (self.agy_dir / "conversations" / f"{cid}.pb").exists(),
                        "has_brain": (self.agy_dir / "brain" / cid).is_dir(),
                    }
                conn.close()
            except Exception as e:
                self.log(f"Note reading conversation_summaries.db: {e}")

        # 2. Also check brain/ folder for any top-level conversations not in DB
        brain_dir = self.agy_dir / "brain"
        if brain_dir.is_dir():
            for child in brain_dir.iterdir():
                if (
                    child.is_dir()
                    and child.name != "tempmediaStorage"
                    and child.name not in convos
                    and child.name not in subagent_ids
                ):
                    cid = child.name
                    mtime = datetime.datetime.fromtimestamp(child.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    title = self._read_pbtxt_title(cid) or f"Conversation {cid[:8]}"
                    convos[cid] = {
                        "id": cid,
                        "title": title,
                        "preview": "",
                        "step_count": 0,
                        "last_modified": mtime,
                        "workspace_uri": "",
                        "workspace_path": "",
                        "has_db": (self.agy_dir / "conversations" / f"{cid}.db").exists()
                        or (self.agy_dir / "conversations" / f"{cid}.pb").exists(),
                        "has_brain": True,
                    }

        return sorted(convos.values(), key=lambda x: x["last_modified"], reverse=True)

    def list_repo_conversations(self, repo_path: str) -> list[dict[str, Any]]:
        """Lists all Antigravity conversations stored inside <repo>/.antigravity_chats."""
        chats_root = Path(repo_path) / REPO_CHATS_DIRNAME
        if not chats_root.is_dir():
            return []

        results: list[dict[str, Any]] = []
        for child in chats_root.iterdir():
            if not child.is_dir() or child.name in ("tempmediaStorage", SYNC_META_DIRNAME):
                continue
            cid = child.name
            meta_dir = child / SYNC_META_DIRNAME
            meta_file = meta_dir / "summary_meta.json"

            title = f"Conversation {cid[:8]}"
            step_count = 0
            last_modified = datetime.datetime.fromtimestamp(child.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            workspace_path = ""
            sync_type = "Brain Only (Legacy)"

            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    s_row = meta.get("summary_row", {})
                    title = s_row.get("title") or s_row.get("preview") or meta.get("annotation_title") or title
                    step_count = s_row.get("step_count", 0)
                    last_modified = str(s_row.get("last_modified_time", last_modified))[:19]
                    ws_uri = extract_first_workspace_uri(s_row.get("workspace_uris", ""))
                    workspace_path = agy_uri_to_path(ws_uri)
                    has_db = (meta_dir / f"{cid}.db").exists() or (meta_dir / f"{cid}.pb").exists()
                    sync_type = "Full State (.db + Brain)" if has_db else "Metadata + Brain"
                except Exception:
                    pass
            else:
                # Fallback: try to infer title/steps from transcript.jsonl or overview.txt
                transcript = child / ".system_generated" / "logs" / "transcript.jsonl"
                if transcript.exists():
                    try:
                        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
                        step_count = len(lines)
                        for line in lines[:5]:
                            obj = json.loads(line)
                            if obj.get("type") == "USER_INPUT":
                                content = obj.get("content", "")
                                m = re.search(r"<USER_REQUEST>\s*(.+?)\s*</USER_REQUEST>", content, re.DOTALL)
                                snippet = (m.group(1) if m else content).strip().splitlines()[0][:60]
                                if snippet:
                                    title = snippet
                                    break
                    except Exception:
                        pass

            results.append(
                {
                    "id": cid,
                    "title": title,
                    "step_count": step_count,
                    "last_modified": last_modified,
                    "workspace_path": workspace_path,
                    "sync_type": sync_type,
                    "folder": str(child),
                }
            )

        return sorted(results, key=lambda x: x["last_modified"], reverse=True)

    def get_conversation_preview_text(self, cid: str, repo_path: str | None = None) -> str:
        """Reads a human-friendly preview of a conversation from either local brain or repo .antigravity_chats."""
        candidates: list[Path] = []
        if repo_path:
            candidates.append(Path(repo_path) / REPO_CHATS_DIRNAME / cid)
        candidates.append(self.agy_dir / "brain" / cid)

        for base in candidates:
            if not base.is_dir():
                continue
            transcript = base / ".system_generated" / "logs" / "transcript.jsonl"
            if transcript.exists():
                try:
                    lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
                    messages: list[str] = []
                    for line in lines:
                        try:
                            obj = json.loads(line)
                            stype = obj.get("type", "")
                            content = obj.get("content", "")
                            if stype == "USER_INPUT":
                                m = re.search(r"<USER_REQUEST>\s*(.+?)\s*</USER_REQUEST>", content, re.DOTALL)
                                txt = (m.group(1) if m else content).strip()
                                messages.append(f"👤 USER: {txt[:300]}")
                            elif stype == "PLANNER_RESPONSE" and content.strip():
                                messages.append(f"🤖 AGENT: {content.strip()[:300]}")
                        except Exception:
                            pass
                    if messages:
                        tail = messages[-8:]
                        return f"--- Conversation {cid} ({len(lines)} total steps) ---\n\n" + "\n\n".join(tail)
                except Exception:
                    pass

            overview = base / ".system_generated" / "logs" / "overview.txt"
            if overview.exists():
                try:
                    txt = overview.read_text(encoding="utf-8", errors="replace")
                    return f"--- Conversation {cid} (Overview Log) ---\n\n" + txt[-1500:]
                except Exception:
                    pass

        return f"Conversation ID: {cid}\n(No transcript preview file found on disk, or binary-only trajectory)."

    # ---- Export (Local Antigravity -> Repo / Custom Folder) ----

    def export_conversation_to_folder(self, cid: str, target_chats_root: Path) -> bool:
        """
        Packs a complete Antigravity conversation (brain + SQLite trajectory DB +
        annotations + conversation_summaries.db rows + any subagent conversations)
        into `target_chats_root/<cid>/`.
        """
        self.log(f"Exporting conversation {cid[:8]}... to {target_chats_root}...")
        dest_conv_dir = target_chats_root / cid
        dest_meta_dir = dest_conv_dir / SYNC_META_DIRNAME
        dest_meta_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy brain/<cid> files into dest_conv_dir
        src_brain = self.agy_dir / "brain" / cid
        if src_brain.is_dir():
            for item in src_brain.iterdir():
                if item.name == SYNC_META_DIRNAME:
                    continue
                target_item = dest_conv_dir / item.name
                try:
                    if item.is_dir():
                        if target_item.exists():
                            shutil.rmtree(target_item)
                        shutil.copytree(item, target_item)
                    else:
                        shutil.copy2(item, target_item)
                except Exception as e:
                    self.log(f"Warning copying brain item {item.name}: {e}")

        # 2. Safely snapshot conversations/<cid>.db (handles live SQLite WAL mode!) or .pb
        src_db = self.agy_dir / "conversations" / f"{cid}.db"
        src_pb = self.agy_dir / "conversations" / f"{cid}.pb"
        if src_db.exists():
            dest_db = dest_meta_dir / f"{cid}.db"
            if dest_db.exists():
                dest_db.unlink()
            try:
                src_conn = sqlite3.connect(str(src_db))
                dst_conn = sqlite3.connect(str(dest_db))
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
            except Exception:
                shutil.copy2(src_db, dest_db)
        elif src_pb.exists():
            shutil.copy2(src_pb, dest_meta_dir / f"{cid}.pb")

        # 3. Copy annotations/<cid>.pbtxt
        ann_title = ""
        src_ann = self.agy_dir / "annotations" / f"{cid}.pbtxt"
        if src_ann.exists():
            shutil.copy2(src_ann, dest_meta_dir / f"{cid}.pbtxt")
            ann_title = self._read_pbtxt_title(cid)

        # 4. Export conversation_summaries.db row & FTS rows + child subagent IDs
        summary_payload: dict[str, Any] = {
            "conversation_id": cid,
            "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
            "annotation_title": ann_title,
            "summary_row": {},
            "fts_rows": [],
            "child_conversation_ids": [],
        }

        summaries_db = self.agy_dir / "conversation_summaries.db"
        if summaries_db.exists():
            try:
                conn = sqlite3.connect(str(summaries_db))
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ?", (cid,)
                ).fetchone()
                if row:
                    row_dict = dict(row)
                    if isinstance(row_dict.get("raw_summary"), (bytes, bytearray)):
                        row_dict["raw_summary_b64"] = base64.b64encode(row_dict.pop("raw_summary")).decode("ascii")
                    else:
                        row_dict.pop("raw_summary", None)
                    summary_payload["summary_row"] = row_dict

                # Export FTS rows for searchability
                fts_rows = conn.execute(
                    "SELECT conversation_id, chunk_index, chunk_first_step, chunk_last_step, "
                    "last_modified_time, step_count, last_step_created_at, archived, "
                    "user_step_byte_offsets, agent_step_byte_offsets, title, workspace, "
                    "project, user_body, agent_body FROM conversations_fts WHERE conversation_id = ?",
                    (cid,),
                ).fetchall()
                serialized_fts = []
                for r in fts_rows:
                    d = {}
                    for k, v in dict(r).items():
                        if isinstance(v, (bytes, bytearray)):
                            d[k] = {"__b64__": base64.b64encode(v).decode("ascii")}
                        else:
                            d[k] = v
                    serialized_fts.append(d)
                summary_payload["fts_rows"] = serialized_fts

                # Check for child subagent conversations
                children = conn.execute(
                    "SELECT conversation_id FROM conversation_summaries WHERE parent_conversation_id = ?",
                    (cid,),
                ).fetchall()
                child_ids = [c["conversation_id"] for c in children]
                summary_payload["child_conversation_ids"] = child_ids
                conn.close()

                # Recursively export child subagent conversations so subagent links work on new device
                for child_id in child_ids:
                    if child_id != cid:
                        self.export_conversation_to_folder(child_id, target_chats_root)
            except Exception as e:
                self.log(f"Warning exporting summary row for {cid[:8]}: {e}")

        meta_file = dest_meta_dir / "summary_meta.json"
        meta_file.write_text(json.dumps(summary_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"Conversation {cid[:8]} ({ann_title or 'chat'}) exported successfully.")
        return True

    # ---- Restore (Repo / Custom Folder -> Local Antigravity) ----

    def restore_conversation_from_folder(
        self,
        src_conv_dir: Path,
        target_workspace_path: str | None = None,
    ) -> bool:
        """
        Restores a packed conversation from `src_conv_dir` directly into this device's
        `~/.gemini/antigravity` directory, automatically remapping the workspace URI
        to `target_workspace_path` so Antigravity links it to the local repo folder!
        """
        if not src_conv_dir.is_dir():
            self.log(f"Error: Source conversation folder does not exist: {src_conv_dir}")
            return False

        cid = src_conv_dir.name
        meta_dir = src_conv_dir / SYNC_META_DIRNAME
        meta_file = meta_dir / "summary_meta.json"
        self.log(f"Restoring conversation {cid[:8]} into local Antigravity ({self.agy_dir})...")

        # Ensure Antigravity directories exist
        brain_dest = self.agy_dir / "brain" / cid
        conv_dir = self.agy_dir / "conversations"
        ann_dir = self.agy_dir / "annotations"
        brain_dest.mkdir(parents=True, exist_ok=True)
        conv_dir.mkdir(parents=True, exist_ok=True)
        ann_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy brain files (excluding .agy_sync_meta)
        for item in src_conv_dir.iterdir():
            if item.name == SYNC_META_DIRNAME:
                continue
            target_item = brain_dest / item.name
            try:
                if item.is_dir():
                    if target_item.exists():
                        shutil.rmtree(target_item)
                    shutil.copytree(item, target_item)
                else:
                    shutil.copy2(item, target_item)
            except Exception as e:
                self.log(f"Warning restoring brain file {item.name}: {e}")

        # Read metadata if available
        meta: dict[str, Any] = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Determine old vs new workspace URIs for smart path remapping across devices
        uri_replacements: list[tuple[bytes, bytes]] = []
        new_ws_uri_encoded = ""
        new_ws_uri_plain = ""
        if target_workspace_path and os.path.isdir(target_workspace_path):
            new_ws_uri_encoded, new_ws_uri_plain = path_to_agy_uris(target_workspace_path)

        s_row: dict[str, Any] = meta.get("summary_row", {})
        old_ws_uri_encoded = extract_first_workspace_uri(s_row.get("workspace_uris", ""))
        if old_ws_uri_encoded and new_ws_uri_encoded and old_ws_uri_encoded != new_ws_uri_encoded:
            old_ws_path = agy_uri_to_path(old_ws_uri_encoded)
            _, old_ws_uri_plain = path_to_agy_uris(old_ws_path)
            uri_replacements = [
                (old_ws_uri_encoded.encode("utf-8"), new_ws_uri_encoded.encode("utf-8")),
                (old_ws_uri_plain.encode("utf-8"), new_ws_uri_plain.encode("utf-8")),
            ]
            self.log(f"Remapping workspace path: {old_ws_uri_plain} -> {new_ws_uri_plain}")

        # 2. Restore conversations/<cid>.db or .pb
        src_db = meta_dir / f"{cid}.db"
        src_pb = meta_dir / f"{cid}.pb"
        if src_db.exists():
            dest_db = conv_dir / f"{cid}.db"
            # Remove stale WAL/SHM files before restoring
            for ext in ("-wal", "-shm"):
                wal_file = conv_dir / f"{cid}.db{ext}"
                if wal_file.exists():
                    try:
                        wal_file.unlink()
                    except Exception:
                        pass
            shutil.copy2(src_db, dest_db)

            # Rewrite trajectory_metadata_blob if workspace path changed on this device
            if uri_replacements:
                try:
                    c_conn = sqlite3.connect(str(dest_db))
                    blobs = c_conn.execute("SELECT id, data FROM trajectory_metadata_blob").fetchall()
                    for bid, bdata in blobs:
                        if isinstance(bdata, (bytes, bytearray)):
                            new_bdata = rewrite_proto_bytes(bytes(bdata), uri_replacements)
                            if new_bdata != bdata:
                                c_conn.execute(
                                    "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?",
                                    (new_bdata, bid),
                                )
                    c_conn.commit()
                    c_conn.close()
                except Exception as e:
                    self.log(f"Note updating trajectory_metadata_blob: {e}")
        elif src_pb.exists():
            shutil.copy2(src_pb, conv_dir / f"{cid}.pb")

        # 3. Restore annotations/<cid>.pbtxt
        src_ann = meta_dir / f"{cid}.pbtxt"
        if src_ann.exists():
            shutil.copy2(src_ann, ann_dir / f"{cid}.pbtxt")

        # 4. Upsert into conversation_summaries.db and agyhub_summaries_proto.pb
        summaries_db = self.agy_dir / "conversation_summaries.db"
        if s_row:
            try:
                raw_bytes = b""
                if "raw_summary_b64" in s_row:
                    raw_bytes = base64.b64decode(s_row.pop("raw_summary_b64"))
                    if uri_replacements:
                        raw_bytes = rewrite_proto_bytes(raw_bytes, uri_replacements)
                s_row["raw_summary"] = raw_bytes

                if new_ws_uri_encoded:
                    s_row["workspace_uris"] = json.dumps([new_ws_uri_encoded])

                conn = sqlite3.connect(str(summaries_db))
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS `conversation_summaries` (
                        `conversation_id` text,
                        `title` text NOT NULL DEFAULT "",
                        `preview` text NOT NULL DEFAULT "",
                        `step_count` integer NOT NULL DEFAULT 0,
                        `last_modified_time` datetime NOT NULL,
                        `workspace_uris` text NOT NULL,
                        `status` text NOT NULL DEFAULT "",
                        `source` text NOT NULL DEFAULT "",
                        `project_id` text NOT NULL DEFAULT "",
                        `agent_name` text NOT NULL DEFAULT "",
                        `parent_conversation_id` text NOT NULL DEFAULT "",
                        `nesting_depth` integer NOT NULL DEFAULT 0,
                        `battle_id` text NOT NULL DEFAULT "",
                        `winning_conversation_id` text NOT NULL DEFAULT "",
                        `not_fully_idle` numeric NOT NULL DEFAULT false,
                        `killed` numeric NOT NULL DEFAULT false,
                        `last_user_input_time` datetime NOT NULL,
                        `last_user_input_step_index` integer NOT NULL DEFAULT -1,
                        `app_data_dir` text NOT NULL DEFAULT "",
                        `raw_summary` blob,
                        `group_id` text NOT NULL DEFAULT "",
                        PRIMARY KEY (`conversation_id`)
                    )"""
                )
                conn.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                        conversation_id UNINDEXED,
                        chunk_index UNINDEXED,
                        chunk_first_step UNINDEXED,
                        chunk_last_step UNINDEXED,
                        last_modified_time UNINDEXED,
                        step_count UNINDEXED,
                        last_step_created_at UNINDEXED,
                        archived UNINDEXED,
                        user_step_byte_offsets UNINDEXED,
                        agent_step_byte_offsets UNINDEXED,
                        title,
                        workspace,
                        project,
                        user_body,
                        agent_body,
                        tokenize = 'unicode61 remove_diacritics 2',
                        prefix = '1 2 3'
                    )"""
                )
                cols = [col[1] for col in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()]
                valid_items = {k: v for k, v in s_row.items() if k in cols}
                col_names = ", ".join(f"`{k}`" for k in valid_items.keys())
                placeholders = ", ".join("?" for _ in valid_items)
                conn.execute(
                    f"INSERT OR REPLACE INTO conversation_summaries ({col_names}) VALUES ({placeholders})",
                    list(valid_items.values()),
                )

                # Restore FTS rows
                fts_rows = meta.get("fts_rows", [])
                if fts_rows:
                    conn.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,))
                    fts_cols = list(fts_rows[0].keys())
                    fts_col_names = ", ".join(fts_cols)
                    fts_placeholders = ", ".join("?" for _ in fts_cols)
                    for frow in fts_rows:
                        vals = []
                        for c in fts_cols:
                            val = frow.get(c)
                            if isinstance(val, dict) and "__b64__" in val:
                                val = base64.b64decode(val["__b64__"])
                            vals.append(val)
                        conn.execute(
                            f"INSERT INTO conversations_fts ({fts_col_names}) VALUES ({fts_placeholders})",
                            vals,
                        )

                conn.commit()
                conn.close()

                # Also sync agyhub_summaries_proto.pb
                pb_path = self.agy_dir / "agyhub_summaries_proto.pb"
                upsert_agyhub_summaries_proto(pb_path, cid, raw_bytes)
            except Exception as e:
                self.log(f"Warning updating conversation_summaries.db: {e}")

        # 5. Also restore any child subagent conversations
        for child_id in meta.get("child_conversation_ids", []):
            child_src = src_conv_dir.parent / child_id
            if child_src.is_dir() and child_id != cid:
                self.restore_conversation_from_folder(child_src, target_workspace_path)

        self.log(f"Conversation {cid[:8]} restored to this device!")
        return True


# ---------------------------------------------------------------------------
# Desktop GUI Application (Tkinter Dark Studio Theme)
# ---------------------------------------------------------------------------

class AgyGitSyncApp:
    BG_DARK = "#181824"
    BG_PANEL = "#222232"
    BG_INPUT = "#2C2C40"
    FG_MAIN = "#ECEFF4"
    FG_MUTED = "#A0A8B8"
    ACCENT_BLUE = "#5E81AC"
    ACCENT_CYAN = "#88C0D0"
    ACCENT_GREEN = "#A3BE8C"
    ACCENT_AMBER = "#EBCB8B"
    ACCENT_PURPLE = "#B48EAD"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Antigravity & Git Sync Studio — Seamless Device Switcher")
        self.root.geometry("1260x820")
        self.root.minsize(1020, 680)
        self.root.configure(bg=self.BG_DARK)

        self.engine = SyncEngine(logger=self._append_log)
        self.repos: list[str] = []
        self.current_repo: str = ""
        self.local_convos: list[dict[str, Any]] = []
        self.repo_convos: list[dict[str, Any]] = []

        self._setup_styles()
        self._build_ui()
        self._initial_load()

    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(".", background=self.BG_DARK, foreground=self.FG_MAIN, font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG_DARK)
        style.configure("Panel.TFrame", background=self.BG_PANEL)
        style.configure("TLabel", background=self.BG_DARK, foreground=self.FG_MAIN)
        style.configure("Panel.TLabel", background=self.BG_PANEL, foreground=self.FG_MAIN)
        style.configure("Muted.TLabel", background=self.BG_PANEL, foreground=self.FG_MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=self.BG_PANEL, foreground=self.ACCENT_CYAN, font=("Segoe UI", 11, "bold"))

        style.configure(
            "Treeview",
            background=self.BG_INPUT,
            foreground=self.FG_MAIN,
            fieldbackground=self.BG_INPUT,
            borderwidth=0,
            rowheight=26,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#32324A",
            foreground=self.ACCENT_CYAN,
            font=("Segoe UI", 9, "bold"),
            borderwidth=1,
        )
        style.map("Treeview", background=[("selected", self.ACCENT_BLUE)], foreground=[("selected", "#FFFFFF")])

        style.configure("TCheckbutton", background=self.BG_PANEL, foreground=self.FG_MAIN)
        style.map("TCheckbutton", background=[("active", self.BG_PANEL)])

        style.configure(
            "TCombobox",
            fieldbackground=self.BG_INPUT,
            background="#32324A",
            foreground=self.FG_MAIN,
            arrowcolor=self.ACCENT_CYAN,
        )

    def _make_btn(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        bg: str = "#383852",
        fg: str = "#ECEFF4",
        bold: bool = False,
        padx: int = 10,
        pady: int = 5,
    ) -> tk.Button:
        font = ("Segoe UI", 10, "bold") if bold else ("Segoe UI", 9)
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=self.ACCENT_BLUE,
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            bd=0,
            padx=padx,
            pady=pady,
            font=font,
            cursor="hand2",
        )
        return btn

    def _build_ui(self) -> None:
        # 1. Top Workflow Banner
        banner = tk.Frame(self.root, bg=self.BG_PANEL, padx=14, pady=10)
        banner.pack(fill=tk.X, padx=10, pady=(10, 6))

        title_col = tk.Frame(banner, bg=self.BG_PANEL)
        title_col.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(
            title_col,
            text="⚡ Antigravity & Git Sync Studio",
            bg=self.BG_PANEL,
            fg=self.ACCENT_CYAN,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_col,
            text="Switch devices seamlessly: sync your Git repositories, uncommitted changes, and active Antigravity chats.",
            bg=self.BG_PANEL,
            fg=self.FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        workflow_col = tk.Frame(banner, bg=self.BG_PANEL)
        workflow_col.pack(side=tk.RIGHT)

        self._make_btn(
            workflow_col,
            "🌅 Start Day on This Device (Pull Git + Restore Chat)",
            self.workflow_start_day,
            bg="#2E5B48",
            fg="#FFFFFF",
            bold=True,
            padx=14,
            pady=8,
        ).pack(side=tk.LEFT, padx=(0, 8))

        self._make_btn(
            workflow_col,
            "🌙 Done for Today (Bundle Chat + Commit & Push to Git)",
            self.workflow_end_day,
            bg="#4C568D",
            fg="#FFFFFF",
            bold=True,
            padx=14,
            pady=8,
        ).pack(side=tk.LEFT)

        # 2. Main Split Content Area (Left: Git Repo Manager | Right: Antigravity Chats Manager)
        main_pane = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            bg=self.BG_DARK,
            sashwidth=6,
            sashrelief=tk.FLAT,
            bd=0,
        )
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # --- LEFT PANEL: Git Repositories & Changes ---
        left_panel = ttk.Frame(main_pane, style="Panel.TFrame", padding=10)
        main_pane.add(left_panel, width=490)

        ttk.Label(left_panel, text="📁 1. Git Repository & App State", style="Header.TLabel").pack(anchor="w")

        repo_bar = tk.Frame(left_panel, bg=self.BG_PANEL)
        repo_bar.pack(fill=tk.X, pady=(6, 4))

        self.repo_combo = ttk.Combobox(repo_bar, state="readonly", font=("Segoe UI", 10))
        self.repo_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.repo_combo.bind("<<ComboboxSelected>>", self._on_repo_selected)

        self._make_btn(repo_bar, "➕ Browse", self.add_repo_folder).pack(side=tk.LEFT, padx=2)
        self._make_btn(repo_bar, "☁️ Clone URL", self.clone_repo_dialog).pack(side=tk.LEFT, padx=2)
        self._make_btn(repo_bar, "🔄", self.refresh_all).pack(side=tk.LEFT, padx=(2, 0))

        # Status Metadata Card
        info_card = tk.Frame(left_panel, bg=self.BG_INPUT, padx=10, pady=8)
        info_card.pack(fill=tk.X, pady=4)

        branch_row = tk.Frame(info_card, bg=self.BG_INPUT)
        branch_row.pack(fill=tk.X)
        self.lbl_branch = tk.Label(branch_row, text="Branch: -", bg=self.BG_INPUT, fg=self.ACCENT_GREEN, font=("Segoe UI", 9, "bold"))
        self.lbl_branch.pack(side=tk.LEFT)
        self._make_btn(
            branch_row,
            "🔗 Init Git / Set Remote URL",
            self.set_remote_or_init_dialog,
            bg="#383852",
            padx=6,
            pady=1,
        ).pack(side=tk.RIGHT)

        self.lbl_remote = tk.Label(info_card, text="Remote: -", bg=self.BG_INPUT, fg=self.FG_MUTED, font=("Segoe UI", 9))
        self.lbl_remote.pack(anchor="w")

        # Working tree changes list
        changes_hdr = tk.Frame(left_panel, bg=self.BG_PANEL)
        changes_hdr.pack(fill=tk.X, pady=(6, 2))
        ttk.Label(changes_hdr, text="Working Tree Changes (Modified / Untracked):", style="Panel.TLabel").pack(side=tk.LEFT)
        self._make_btn(changes_hdr, "📂 Open Folder", self.open_repo_in_explorer, pady=2, padx=6).pack(side=tk.RIGHT)

        tree_frame = tk.Frame(left_panel, bg=self.BG_PANEL)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=2)

        self.changes_tree = ttk.Treeview(
            tree_frame,
            columns=("status", "file"),
            show="headings",
            selectmode="extended",
            height=9,
        )
        self.changes_tree.heading("status", text="State")
        self.changes_tree.heading("file", text="File Path")
        self.changes_tree.column("status", width=60, anchor="center")
        self.changes_tree.column("file", width=380, anchor="w")
        self.changes_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ch_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.changes_tree.yview)
        self.changes_tree.configure(yscrollcommand=ch_scroll.set)
        ch_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Commit message & Git action buttons
        ttk.Label(left_panel, text="Commit Message:", style="Panel.TLabel").pack(anchor="w", pady=(6, 2))
        self.commit_entry = tk.Entry(
            left_panel,
            bg=self.BG_INPUT,
            fg=self.FG_MAIN,
            insertbackground=self.FG_MAIN,
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        self.commit_entry.pack(fill=tk.X, ipady=5, pady=(0, 6))
        self.commit_entry.insert(0, "chore: sync app state and Antigravity conversation")

        git_btns1 = tk.Frame(left_panel, bg=self.BG_PANEL)
        git_btns1.pack(fill=tk.X, pady=2)
        self._make_btn(
            git_btns1,
            "💾 Commit All Changes",
            lambda: self.commit_action(push=False),
            bg="#3B4252",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        self._make_btn(
            git_btns1,
            "🚀 Commit & Push to Git",
            lambda: self.commit_action(push=True),
            bg=self.ACCENT_BLUE,
            bold=True,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)
        self._make_btn(
            git_btns1,
            "⬇️ Git Pull",
            self.pull_action,
            bg="#3B4252",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        git_btns2 = tk.Frame(left_panel, bg=self.BG_PANEL)
        git_btns2.pack(fill=tk.X, pady=(4, 0))
        self._make_btn(
            git_btns2,
            "📋 Copy Changed Files to Folder...",
            lambda: self.copy_files_dialog(only_changed=True),
            bg="#2F3545",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        self._make_btn(
            git_btns2,
            "📦 Copy Entire Repo to Folder...",
            lambda: self.copy_files_dialog(only_changed=False),
            bg="#2F3545",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        # --- RIGHT PANEL: Antigravity Conversations ---
        right_panel = ttk.Frame(main_pane, style="Panel.TFrame", padding=10)
        main_pane.add(right_panel, width=740)

        r_hdr = tk.Frame(right_panel, bg=self.BG_PANEL)
        r_hdr.pack(fill=tk.X)
        ttk.Label(
            r_hdr,
            text="💬 2. Antigravity Conversations (Device <-> Repository Sync)",
            style="Header.TLabel",
        ).pack(side=tk.LEFT)

        self.filter_repo_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            r_hdr,
            text="Show only chats for selected repo",
            variable=self.filter_repo_only_var,
            command=self._populate_local_convos_tree,
        ).pack(side=tk.RIGHT)

        # Local Device Conversations Table
        ttk.Label(
            right_panel,
            text="🖥️ Conversations on This Device (~/.gemini/antigravity) — Select chat(s) to bundle into repo:",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 2))

        local_frame = tk.Frame(right_panel, bg=self.BG_PANEL)
        local_frame.pack(fill=tk.BOTH, expand=True)

        self.local_tree = ttk.Treeview(
            local_frame,
            columns=("match", "title", "steps", "modified", "workspace", "id"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        self.local_tree.heading("match", text="Repo")
        self.local_tree.heading("title", text="Conversation Title")
        self.local_tree.heading("steps", text="Steps")
        self.local_tree.heading("modified", text="Last Active")
        self.local_tree.heading("workspace", text="Linked Workspace")
        self.local_tree.heading("id", text="Conversation ID")

        self.local_tree.column("match", width=55, anchor="center")
        self.local_tree.column("title", width=220, anchor="w")
        self.local_tree.column("steps", width=55, anchor="center")
        self.local_tree.column("modified", width=130, anchor="center")
        self.local_tree.column("workspace", width=160, anchor="w")
        self.local_tree.column("id", width=120, anchor="w")
        self.local_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.local_tree.bind("<<TreeviewSelect>>", self._on_local_conv_selected)

        l_scroll = ttk.Scrollbar(local_frame, orient=tk.VERTICAL, command=self.local_tree.yview)
        self.local_tree.configure(yscrollcommand=l_scroll.set)
        l_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Transfer Buttons between Local Device & Repo
        transfer_bar = tk.Frame(right_panel, bg=self.BG_PANEL, pady=6)
        transfer_bar.pack(fill=tk.X)

        self._make_btn(
            transfer_bar,
            "⬇️ Copy Selected Local Chat(s) -> Repo (.antigravity_chats)",
            self.copy_selected_local_to_repo,
            bg=self.ACCENT_BLUE,
            bold=True,
        ).pack(side=tk.LEFT, padx=(0, 6))

        self._make_btn(
            transfer_bar,
            "⬆️ Restore Selected Repo Chat(s) -> This Device's Antigravity",
            self.restore_selected_repo_to_local,
            bg="#2E5B48",
            bold=True,
        ).pack(side=tk.LEFT, padx=6)

        self._make_btn(
            transfer_bar,
            "📤 Export/Import External Folder...",
            self.custom_folder_chat_dialog,
            bg="#383852",
        ).pack(side=tk.RIGHT)

        # Repo-Stored Conversations Table
        ttk.Label(
            right_panel,
            text="📦 Conversations Stored in Selected Git Repo (<repo>/.antigravity_chats) — Select chat(s) to restore:",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 2))

        repo_c_frame = tk.Frame(right_panel, bg=self.BG_PANEL)
        repo_c_frame.pack(fill=tk.BOTH, expand=True)

        self.repo_tree = ttk.Treeview(
            repo_c_frame,
            columns=("type", "title", "steps", "modified", "id"),
            show="headings",
            selectmode="extended",
            height=5,
        )
        self.repo_tree.heading("type", text="Backup Format")
        self.repo_tree.heading("title", text="Conversation Title")
        self.repo_tree.heading("steps", text="Steps")
        self.repo_tree.heading("modified", text="Last Saved")
        self.repo_tree.heading("id", text="Conversation ID")

        self.repo_tree.column("type", width=140, anchor="center")
        self.repo_tree.column("title", width=240, anchor="w")
        self.repo_tree.column("steps", width=55, anchor="center")
        self.repo_tree.column("modified", width=130, anchor="center")
        self.repo_tree.column("id", width=150, anchor="w")
        self.repo_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.repo_tree.bind("<<TreeviewSelect>>", self._on_repo_conv_selected)

        rc_scroll = ttk.Scrollbar(repo_c_frame, orient=tk.VERTICAL, command=self.repo_tree.yview)
        self.repo_tree.configure(yscrollcommand=rc_scroll.set)
        rc_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Conversation Transcript Preview Box
        ttk.Label(right_panel, text="🔍 Selected Conversation Preview:", style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
        self.preview_text = tk.Text(
            right_panel,
            height=6,
            bg=self.BG_INPUT,
            fg=self.FG_MAIN,
            font=("Consolas", 9),
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=8,
            pady=6,
        )
        self.preview_text.pack(fill=tk.X)

        # 3. Bottom Activity Log Console
        log_frame = tk.Frame(self.root, bg=self.BG_PANEL, padx=10, pady=6)
        log_frame.pack(fill=tk.X, padx=10, pady=(4, 10))

        ttk.Label(log_frame, text="📜 Activity & Sync Log:", style="Muted.TLabel").pack(anchor="w")
        self.log_text = tk.Text(
            log_frame,
            height=4,
            bg="#14141E",
            fg=self.ACCENT_GREEN,
            font=("Consolas", 9),
            relief=tk.FLAT,
            padx=8,
            pady=4,
        )
        self.log_text.pack(fill=tk.X, pady=(2, 0))

    # ---- Logging & Helpers ----

    def _append_log(self, msg: str) -> None:
        def _write() -> None:
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)

        if threading.current_thread() is threading.main_thread():
            _write()
        else:
            self.root.after(0, _write)

    def _set_preview(self, text: str) -> None:
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, text)

    def _initial_load(self) -> None:
        self.repos = self.engine.discover_git_repos()
        cfg = self.engine.load_config()
        last_repo = cfg.get("last_repo", "")

        # Prefer current directory TP if available
        default_tp = os.path.normpath(str(Path(__file__).resolve().parent))
        if default_tp in self.repos:
            self.current_repo = default_tp
        elif last_repo in self.repos:
            self.current_repo = last_repo
        elif self.repos:
            self.current_repo = self.repos[0]

        self.repo_combo["values"] = self.repos
        if self.current_repo:
            self.repo_combo.set(self.current_repo)

        self.refresh_all()
        self.engine.log("Ready! Select a Git repository and Antigravity conversation to sync.")

    def refresh_all(self) -> None:
        self.local_convos = self.engine.list_local_conversations()
        self._refresh_repo_panel()
        self._populate_local_convos_tree()
        self._populate_repo_convos_tree()

    def _on_repo_selected(self, _event: Any = None) -> None:
        sel = self.repo_combo.get()
        if sel and os.path.isdir(sel):
            self.current_repo = os.path.normpath(sel)
            cfg = self.engine.load_config()
            cfg["last_repo"] = self.current_repo
            self.engine.save_config(cfg)
            self.refresh_all()

    def _refresh_repo_panel(self) -> None:
        for item in self.changes_tree.get_children():
            self.changes_tree.delete(item)

        if not self.current_repo:
            self.lbl_branch.config(text="Branch: None selected")
            self.lbl_remote.config(text="Remote: -")
            return

        status = self.engine.get_repo_status(self.current_repo)
        self.lbl_branch.config(text=f"Branch: {status['branch']}   ({len(status['changes'])} uncommitted change(s))")
        self.lbl_remote.config(text=f"Remote: {status['remote']}")

        for st, filepath in status["changes"]:
            self.changes_tree.insert("", tk.END, values=(st, filepath))

    def _is_conv_for_current_repo(self, conv: dict[str, Any]) -> bool:
        if not self.current_repo or not conv.get("workspace_path"):
            return False
        try:
            return os.path.normcase(os.path.normpath(conv["workspace_path"])) == os.path.normcase(
                os.path.normpath(self.current_repo)
            )
        except Exception:
            return False

    def _populate_local_convos_tree(self) -> None:
        for item in self.local_tree.get_children():
            self.local_tree.delete(item)

        only_repo = self.filter_repo_only_var.get()
        first_matching_iid = None

        for conv in self.local_convos:
            matches = self._is_conv_for_current_repo(conv)
            if only_repo and not matches:
                continue
            badge = "⭐ YES" if matches else ""
            ws_short = Path(conv["workspace_path"]).name if conv.get("workspace_path") else "-"
            iid = conv["id"]
            self.local_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(badge, conv["title"], conv["step_count"], conv["last_modified"], ws_short, iid),
            )
            if matches and first_matching_iid is None:
                first_matching_iid = iid

        if first_matching_iid:
            self.local_tree.selection_set(first_matching_iid)
            self.local_tree.see(first_matching_iid)

    def _populate_repo_convos_tree(self) -> None:
        for item in self.repo_tree.get_children():
            self.repo_tree.delete(item)

        if not self.current_repo:
            return

        self.repo_convos = self.engine.list_repo_conversations(self.current_repo)
        for idx, conv in enumerate(self.repo_convos):
            iid = conv["id"]
            self.repo_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(conv["sync_type"], conv["title"], conv["step_count"], conv["last_modified"], iid),
            )
            if idx == 0:
                self.repo_tree.selection_set(iid)

    def _on_local_conv_selected(self, _event: Any = None) -> None:
        sel = self.local_tree.selection()
        if sel:
            cid = sel[0]
            self._set_preview(self.engine.get_conversation_preview_text(cid, self.current_repo))

    def _on_repo_conv_selected(self, _event: Any = None) -> None:
        sel = self.repo_tree.selection()
        if sel:
            cid = sel[0]
            self._set_preview(self.engine.get_conversation_preview_text(cid, self.current_repo))

    # ---- User Actions & Dialogs ----

    def add_repo_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select Git Repository Folder")
        if folder:
            norm = os.path.normpath(folder)
            if norm not in self.repos:
                self.repos.append(norm)
                self.repos.sort()
                self.repo_combo["values"] = self.repos
            self.repo_combo.set(norm)
            self._on_repo_selected()

    def clone_repo_dialog(self) -> None:
        url = simpledialog.askstring(
            "Clone Git Repository",
            "Enter GitHub / Git repository URL to clone onto this device:",
            parent=self.root,
        )
        if not url or not url.strip():
            return
        parent_dir = filedialog.askdirectory(title="Select Parent Directory Where Repo Should Be Cloned")
        if not parent_dir:
            return
        repo_name = url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        target_dir = os.path.join(parent_dir, repo_name)

        def _work() -> None:
            ok = self.engine.git_clone(url.strip(), target_dir)
            if ok:
                def _done() -> None:
                    norm = os.path.normpath(target_dir)
                    if norm not in self.repos:
                        self.repos.append(norm)
                        self.repo_combo["values"] = self.repos
                    self.repo_combo.set(norm)
                    self._on_repo_selected()
                    messagebox.showinfo("Clone Complete", f"Repository cloned to:\n{norm}")
                self.root.after(0, _done)

        threading.Thread(target=_work, daemon=True).start()

    def open_repo_in_explorer(self) -> None:
        if self.current_repo and os.path.isdir(self.current_repo):
            os.startfile(self.current_repo) if os.name == "nt" else subprocess.Popen(["xdg-open", self.current_repo])

    def set_remote_or_init_dialog(self) -> None:
        if not self.current_repo or not os.path.isdir(self.current_repo):
            messagebox.showwarning("No Folder Selected", "Please select or browse to a folder first using '➕ Browse'.")
            return

        status = self.engine.get_repo_status(self.current_repo)
        current_remote = status["remote"] if status["remote"] != "No remote configured" else ""

        url = simpledialog.askstring(
            "Initialize Git & Set Remote URL",
            f"Folder: {self.current_repo}\n\nEnter the GitHub repository URL (e.g. https://github.com/user/repo.git):",
            initialvalue=current_remote,
            parent=self.root,
        )
        if url is None:
            return
        url = url.strip()

        if not status["is_git"]:
            self.engine.log(f"Initializing new Git repository in {self.current_repo}...")
            self.engine.run_git(self.current_repo, ["init"])
            self.engine.run_git(self.current_repo, ["branch", "-M", "main"])

        if url:
            # Check if 'origin' already exists
            code, out, _ = self.engine.run_git(self.current_repo, ["remote"])
            if code == 0 and "origin" in out.splitlines():
                self.engine.run_git(self.current_repo, ["remote", "set-url", "origin", url])
                self.engine.log(f"Updated remote 'origin' URL to: {url}")
            else:
                self.engine.run_git(self.current_repo, ["remote", "add", "origin", url])
                self.engine.log(f"Added remote 'origin' URL: {url}")

        self.refresh_all()
        messagebox.showinfo(
            "Git Repository Configured",
            f"Folder: {self.current_repo}\nRemote: {url or 'Local only'}\n\n"
            "You can now click '🚀 Commit & Push to Git' or '🌙 Done for Today'!",
        )

    def commit_action(self, push: bool = False) -> None:
        if not self.current_repo:
            messagebox.showwarning("No Repository", "Please select a Git repository first.")
            return
        msg = self.commit_entry.get().strip()

        def _work() -> None:
            ok = self.engine.git_commit_and_push(self.current_repo, msg, push=push)
            self.root.after(0, self.refresh_all)
            if ok:
                action_label = "Committed & pushed" if push else "Committed"
                self.root.after(0, lambda: messagebox.showinfo("Git Success", f"{action_label} changes successfully!"))
            else:
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Git Warning", "Git operation encountered an issue. Check the Activity Log at the bottom."
                    ),
                )

        threading.Thread(target=_work, daemon=True).start()

    def pull_action(self) -> None:
        if not self.current_repo:
            return

        def _work() -> None:
            self.engine.git_pull(self.current_repo)
            self.root.after(0, self.refresh_all)

        threading.Thread(target=_work, daemon=True).start()

    def copy_files_dialog(self, only_changed: bool = True) -> None:
        if not self.current_repo:
            return
        dest = filedialog.askdirectory(
            title="Select Destination Folder to Copy Changed Files" if only_changed else "Select Destination Folder to Copy Repo"
        )
        if not dest:
            return
        count = self.engine.copy_repo_changes_to_folder(self.current_repo, dest, only_changed=only_changed)
        messagebox.showinfo("Copy Complete", f"Copied {count} file(s) to:\n{dest}")

    def copy_selected_local_to_repo(self) -> None:
        if not self.current_repo:
            messagebox.showwarning("No Repository", "Please select a target Git repository first.")
            return
        sel = list(self.local_tree.selection())
        if not sel:
            messagebox.showwarning(
                "No Conversation Selected",
                "Please select one or more conversations in the 'Conversations on This Device' table.",
            )
            return

        target_chats_root = Path(self.current_repo) / REPO_CHATS_DIRNAME
        for cid in sel:
            self.engine.export_conversation_to_folder(cid, target_chats_root)

        self.refresh_all()
        messagebox.showinfo(
            "Chat Bundled into Repo",
            f"Exported {len(sel)} conversation(s) (with full SQLite trajectory DB, brain artifacts, and metadata) into:\n{target_chats_root}",
        )

    def restore_selected_repo_to_local(self) -> None:
        if not self.current_repo:
            return
        sel = list(self.repo_tree.selection())
        if not sel:
            messagebox.showwarning(
                "No Repo Chat Selected",
                "Please select one or more conversations in the 'Conversations Stored in Selected Git Repo' table.",
            )
            return

        chats_root = Path(self.current_repo) / REPO_CHATS_DIRNAME
        restored = 0
        for cid in sel:
            src_dir = chats_root / cid
            if self.engine.restore_conversation_from_folder(src_dir, target_workspace_path=self.current_repo):
                restored += 1

        self.refresh_all()
        messagebox.showinfo(
            "Conversations Restored!",
            f"Restored {restored} conversation(s) into local Antigravity and linked them to:\n{self.current_repo}\n\n"
            "Tip: If Antigravity is currently open, reload/restart the window to see the newly restored conversation state.",
        )

    def custom_folder_chat_dialog(self) -> None:
        choice = messagebox.askquestion(
            "Export or Import Chat Folder",
            "Click 'Yes' to EXPORT selected local chat(s) to an external folder (e.g. USB drive).\n"
            "Click 'No' to IMPORT a chat folder from an external directory into Antigravity.",
        )
        if choice == "yes":
            sel = list(self.local_tree.selection())
            if not sel:
                messagebox.showwarning("Select Chat", "Select a local conversation first.")
                return
            dest = filedialog.askdirectory(title="Select External Folder to Export Conversation(s) Into")
            if dest:
                for cid in sel:
                    self.engine.export_conversation_to_folder(cid, Path(dest))
                messagebox.showinfo("Export Complete", f"Exported {len(sel)} conversation(s) to {dest}")
        else:
            src = filedialog.askdirectory(title="Select Specific Conversation Folder (UUID folder) to Import")
            if src:
                self.engine.restore_conversation_from_folder(Path(src), target_workspace_path=self.current_repo)
                self.refresh_all()
                messagebox.showinfo("Import Complete", f"Imported conversation from {src} into local Antigravity!")

    # ---- One-Click Day Workflows ----

    def workflow_start_day(self) -> None:
        """
        One-Click 'Start Day on This Device':
        1. Runs `git pull` on the selected repository to grab latest code + `.antigravity_chats`.
        2. Restores selected (or all) conversations from `<repo>/.antigravity_chats` into local Antigravity,
           automatically remapping the workspace path to this device's repo folder!
        """
        if not self.current_repo:
            messagebox.showwarning("No Repo Selected", "Please select or clone your Git repository first.")
            return

        sel_repo_chats = list(self.repo_tree.selection())

        def _work() -> None:
            self.engine.log("=== 🌅 STARTING DAY SETUP ON THIS DEVICE ===")
            self.engine.git_pull(self.current_repo)

            repo_chats = self.engine.list_repo_conversations(self.current_repo)
            to_restore = sel_repo_chats if sel_repo_chats else [c["id"] for c in repo_chats]

            chats_root = Path(self.current_repo) / REPO_CHATS_DIRNAME
            restored_count = 0
            for cid in to_restore:
                src_dir = chats_root / cid
                if src_dir.is_dir():
                    if self.engine.restore_conversation_from_folder(src_dir, target_workspace_path=self.current_repo):
                        restored_count += 1

            self.engine.log(f"=== ✅ START DAY COMPLETE: {restored_count} conversation(s) synced to this device ===")
            self.root.after(0, self.refresh_all)
            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "🌅 Device Ready!",
                    f"1. Pulled latest repository state in:\n   {self.current_repo}\n\n"
                    f"2. Restored {restored_count} Antigravity conversation(s) and linked them to this device's workspace path.\n\n"
                    "You can now open your project & Antigravity chat right where you left off!",
                ),
            )

        threading.Thread(target=_work, daemon=True).start()

    def workflow_end_day(self) -> None:
        """
        One-Click 'Done for Today':
        1. Exports the selected local Antigravity conversation(s) (or all conversations matching this repo)
           into `<repo>/.antigravity_chats/`.
        2. Stages all app changes + `.antigravity_chats/`, commits with the commit message, and pushes to GitHub!
        """
        if not self.current_repo:
            messagebox.showwarning("No Repo Selected", "Please select your Git repository first.")
            return

        sel_local = list(self.local_tree.selection())
        if not sel_local:
            # Automatically pick conversations linked to this repo
            sel_local = [c["id"] for c in self.local_convos if self._is_conv_for_current_repo(c)]

        if not sel_local:
            messagebox.showwarning(
                "Select Conversation",
                "Please select the Antigravity conversation you want to save in the 'Conversations on This Device' table.",
            )
            return

        msg = self.commit_entry.get().strip()
        if not msg:
            msg = f"chore: end of day sync - app state & Antigravity chat ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})"

        def _work() -> None:
            self.engine.log("=== 🌙 SAVING END-OF-DAY STATE TO GIT ===")
            target_chats_root = Path(self.current_repo) / REPO_CHATS_DIRNAME
            for cid in sel_local:
                self.engine.export_conversation_to_folder(cid, target_chats_root)

            ok = self.engine.git_commit_and_push(self.current_repo, msg, push=True)
            self.root.after(0, self.refresh_all)
            if ok:
                self.engine.log("=== ✅ END OF DAY SYNC COMPLETE: App & chats pushed to Git! ===")
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "🌙 Saved & Pushed to Git!",
                        f"Successfully bundled {len(sel_local)} Antigravity conversation(s) into .antigravity_chats,\n"
                        f"committed all app changes, and pushed to remote!\n\n"
                        "You can now switch to any other device and click 'Start Day on This Device'.",
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Committed Locally (Check Push)",
                        "Conversations were exported to .antigravity_chats and staged/committed locally, "
                        "but pushing to remote reported an issue. Check the Activity Log below.",
                    ),
                )

        threading.Thread(target=_work, daemon=True).start()


# ---------------------------------------------------------------------------
# CLI Entrypoint & GUI Launcher
# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Antigravity & Git Sync Studio")
    parser.add_argument("--status", action="store_true", help="Print discovered repos and local conversations")
    parser.add_argument("--repo", type=str, default="", help="Target repository path for CLI commands")
    parser.add_argument("--export-chat", type=str, default="", help="Conversation ID (or 'repo' for all repo chats) to export into repo")
    parser.add_argument("--restore-chats", action="store_true", help="Restore chats from repo's .antigravity_chats into local Antigravity")
    parser.add_argument("--commit-push", type=str, default="", help="Commit and push repo with message")
    args = parser.parse_args()

    if args.status or args.export_chat or args.restore_chats or args.commit_push:
        engine = SyncEngine()
        repo = os.path.normpath(args.repo) if args.repo else os.path.normpath(str(Path(__file__).resolve().parent))
        if args.status:
            print("=== Discovered Git Repositories ===")
            for r in engine.discover_git_repos():
                st = engine.get_repo_status(r)
                print(f" - {r} [{st['branch']}] ({len(st['changes'])} changes)")
            print("\n=== Local Antigravity Conversations ===")
            for c in engine.list_local_conversations():
                print(f" - {c['id']} | {c['title']} | steps={c['step_count']} | ws={c['workspace_path']}")
            print(f"\n=== Repo Conversations in {repo} ===")
            for c in engine.list_repo_conversations(repo):
                print(f" - {c['id']} | {c['title']} | {c['sync_type']} | steps={c['step_count']}")

        if args.export_chat:
            target_root = Path(repo) / REPO_CHATS_DIRNAME
            if args.export_chat.lower() == "all":
                for c in engine.list_local_conversations():
                    if os.path.normcase(c["workspace_path"]) == os.path.normcase(repo):
                        engine.export_conversation_to_folder(c["id"], target_root)
            else:
                engine.export_conversation_to_folder(args.export_chat, target_root)

        if args.restore_chats:
            chats_root = Path(repo) / REPO_CHATS_DIRNAME
            for c in engine.list_repo_conversations(repo):
                engine.restore_conversation_from_folder(chats_root / c["id"], target_workspace_path=repo)

        if args.commit_push:
            engine.git_commit_and_push(repo, args.commit_push, push=True)
        return

    root = tk.Tk()
    AgyGitSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
