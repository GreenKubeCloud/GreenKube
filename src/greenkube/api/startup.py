# src/greenkube/api/startup.py
"""
Background work of the API process: none (roadmap M8 process topology).

API replicas are stateless request handlers, safe to scale horizontally. The API
lifespan only connects the database, builds request-scoped services and bootstraps
the first admin account. Every periodic job runs in the controller process under
its loop registry (``greenkube.core.loops``):

- recommendation scans (E1); API replicas only enqueue runs through
  ``POST /api/v1/optimization/runs``;
- plan expiry, exhausted-operation reconciliation and automation history retention,
  which used to run in every API replica, now run in the leader-gated automation
  controller loop (``greenkube.automation.controller``);
- plan execution (also the optional dedicated worker), the webhook outbox, pull-request
  polling, branch cleanup and the session purge.
"""

#: Names of the periodic tasks the API process starts. Kept empty on purpose: a test
#: guards that no background mutation creeps back into the API lifespan.
API_BACKGROUND_LOOPS: tuple[str, ...] = ()
