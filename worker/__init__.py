"""Outbox worker: claims webhook_outbox jobs and dispatches them.

A separate process from the gateway (python -m worker). It talks to the
GATEWAY database (that is where the job queue lives) and, from increment 5
on, starts LangGraph agent runs for dispute jobs.
"""
