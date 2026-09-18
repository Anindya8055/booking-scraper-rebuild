# Booking Scraper Rebuild

## Project Status

This is a fresh project.

The repository is standalone. Do not inspect, copy, import, or inherit project history, code, configuration, instructions, assumptions, or architecture from outside this repository unless explicitly instructed.

## Core Objective

The system must ultimately retrieve and process REAL Booking.com production hotel-search data.

Real production data is the only valid proof of the retrieval system working.

## Data Integrity

Never create or use fake, invented, synthetic, placeholder, mock, or demo Booking.com hotel data as evidence that the system works.

Tests involving retrieval must use real external data when retrieval behavior is being validated.

Local test data may only be introduced later when there is a specific regression-testing reason and it is clearly identified as test data.

## Research-First Rule

Existing open-source/local projects that claim to retrieve real Booking.com data will be investigated and evaluated before implementing a new retrieval mechanism.

Do not invent a retrieval architecture before the candidate-project research is complete.

## Agent Behavior

Do not declare success based on plans, documentation, fixtures, mocked responses, or partial implementation.

Any claim about working real Booking.com retrieval must be supported by independently observable evidence.

Keep research, implementation, and verification separate.

Record important evidence and decisions in the repository.

## Current Phase

Environment and research setup only.

Do not build the scraper implementation yet.

Do not choose the final architecture yet.

## Agent Roles

- Hermes: project coordinator and orchestration layer.
- OpenCode: primary implementation engineer.
- Goose: automation and supporting execution.
- GPT Researcher: research and candidate-project investigation.

These are the project agents for this repository.

Do not introduce additional agents or external project instructions unless explicitly directed.
