# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The migration transaction guard also catches a `COMMIT` that shares a line
  with another statement.

## [0.2.0] - 2026-08-24

### Added

- Tamper-evident hash chain over the room event log: every appended event
  commits to its predecessor, and `python -m multiplayer.manage <db> audit
  verify` recomputes the chains and names the first divergence per room.
- Database-backed credentials: hashed at rest, one row per token, revocable
  without a restart, managed by the operator CLI (`user add`, `token mint`,
  `token revoke`, `token list`).
- A WAL reader pool: a read outside a transaction no longer waits behind an
  unrelated write transaction (previously measured blocking over a second).
- The MIT `LICENSE` file the project metadata already claimed.

### Changed

- `MULTIAI_AUTH_TOKENS` is bootstrap-only. Authentication reads the
  `user_tokens` table on every request, so a revocation takes effect on the
  next call; a revoked bootstrap token stays revoked across restarts.
- A live WebSocket re-authenticates on its heartbeat and closes within about
  two beats of its credential being revoked.
- The server reports the installed package version instead of a hardcoded one.

### Fixed

- Migrations now apply atomically with the row that records them: a crash
  mid-migration leaves the database exactly at the previous migration, and a
  migration that manages its own transaction is refused outright.
- Removed the vendored `.agents` bundle: 125 unlicensed third-party files
  referenced by nothing, conflicting with the MIT claim.

## [0.1.0] - 2026-08-23

Initial workspace: event-sourced rooms with WebSocket sync, conversation
layer, five-way capability terms with a tool gateway and human approval,
agent identity and harness protocol, room postures, decision workflow, and
the lazy ontology with Meta answers.
