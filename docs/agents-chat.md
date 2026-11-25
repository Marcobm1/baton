# Agents chat design

The current Chat page streams local model replies. Tool-enabled chat is pending a confined execution environment on the Windows host.

## Working directory and permissions

Starting an agent in a repository sets the base for relative paths. A child process can still open an absolute path with the Windows user's privileges. The existing OpenCode runner has demonstrated writes outside its starting directory. Codex's installed help also specifies external isolation for its bypass mode. Tailscale restricts access to Baton but cannot constrain commands Baton starts.

Run the tool process and its descendants inside an OS boundary with the selected repository or scratch directory as its only host workspace. Keep the hub's environment, data, Docker socket and host profiles out of reach. The existing Compose service mounts Baton data and Codex session logs and is unsuitable as the agent worker. CLI login and session state need a separate design that keeps credentials out of both Baton and the tool terminal.

Verify isolation with two temporary sibling directories, one mounted as the workspace and one holding a marker. An approved command and a child process should each be able to edit the workspace and be denied access to the sibling, including through a junction. Repeat in Full mode. Disable tool execution if the boundary cannot be established.

## Conversation transport

Prefer OpenCode ACP over stdio for the first implementation. One agent process can belong to one conversation without opening a listening port. Pass user text as protocol data. Bind the session, canonical folder, permission mode and model on the server. Stream tool arguments and results as text. Bind each Allow or Deny response to one pending tool request in that session and protect it with Baton's CSRF checks.

The Windows PATH check found neither a Claude ACP adapter nor a Codex ACP adapter. Codex exec supports stdin, JSON events and session resume. Its help did not advertise a bidirectional approval channel. Verify the approval exchange with a real client before exposing Confirm mode. A log event emitted after a command starts is too late for authorization. Verify Claude's streaming permission exchange separately.

Full mode should preapprove calls only inside the same boundary. Display a red warning and conversation badge, and reject Full on the server for abliterated models, including when selected via their OpenCode provider alias. Fake agents can verify the chat protocol and UI state. Host confinement and phone access require separate acceptance checks.
