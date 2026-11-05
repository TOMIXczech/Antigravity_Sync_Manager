# ⚡ Antigravity & Git Sync Studio

A standalone, zero-dependency desktop application to manage your Git repositories, commit/push project state, and seamlessly transfer **Antigravity conversations** across devices as if you never left.

## ✨ Features

* **Auto-Discovers Git Repositories:** Scans your PC and Antigravity workspaces to list all your Git projects, branches, remotes, and uncommitted file changes.
* **Full Antigravity Conversation Sync:** Packs and restores:
  * Live SQLite trajectory databases (`conversations/<id>.db`) using safe online SQLite backup
  * Conversation titles & annotations (`annotations/<id>.pbtxt`)
  * Summary & search index records (`conversation_summaries.db` & `agyhub_summaries_proto.pb`)
  * Artifacts, logs, and transcripts (`brain/<id>/`)
* **Smart Cross-Device Path Remapping:** Automatically updates the workspace path inside Antigravity's SQLite & protobuf metadata when restoring on a different PC (even if your Windows username or drive path differs).
* **One-Click Daily Workflows:**
  * **🌙 Done for Today:** Bundles selected Antigravity chat(s) into `<repo>/.antigravity_chats`, commits all project changes, and pushes to GitHub.
  * **🌅 Start Day on This Device:** Pulls the latest commits from GitHub and restores the repository's Antigravity conversations into `~/.gemini/antigravity`.

## 🚀 Quick Start

1. Clone this repository on any device:
   ```bash
   git clone https://github.com/TOMIXczech/Antigravity_Sync_Manager.git
   ```
2. Double-click **`Start_Sync_Manager.bat`** (or run `python agy_git_sync.py`).
