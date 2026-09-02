# Talking to other agents

XYZZY speaks Google's [A2A](https://a2a-protocol.org/) v0.3.0, so an agent built
against somebody else's runtime can be asked for work here, and one of ours can
ask it back.

`GET /.well-known/agent-card.json` is the discovery document and needs no
credential. It advertises the door and **no agents at all**: a room's membership
is the access-control decision, so a public list of agents and their skills
would publish the shape of a private workspace to anyone who fetched a URL. The
authenticated `agent/getAuthenticatedExtendedCard` shows each caller only the
agents that caller could actually address, which means no two callers share one
document.

`POST /a2a/v1` is the JSON-RPC 2.0 endpoint: `message/send`, `message/stream`,
`tasks/get`, `tasks/cancel`, `tasks/resubscribe`,
`agent/getAuthenticatedExtendedCard`, and the two `tasks/pushNotificationConfig`
methods. The card advertises `pushNotifications: false` and those two refuse by
name, because a webhook fan-out would be a second delivery path with weaker guarantees
than the durable ordered log clients already have. Streaming is
Server-Sent-Events over that same log, not a parallel one.

A2A addresses one agent per URL and this server fronts many rooms, so
`message.metadata` carries `roomId` and `targetAgentId`. A caller who may not act
in a room gets the same refusal whether the agent is real, filed elsewhere, or
imaginary; a task you may not read answers exactly as a task that does not exist.

Two rules about delegation are worth knowing before you wire agents to each
other. What a delegate may spend is its asker's own authority intersected with
its own, re-read from durable rows at the moment of spending: narrow the asker
mid-task and the delegate narrows with it, and an asker that has left the room
lends nothing. And the chain a delegation belongs to is read from the delegating
agent's own open run rather than taken from the request, so an agent cannot
start a fresh chain by declining to name its parent: a cycle is refused by name,
and a chain deeper than four delegations is too.
