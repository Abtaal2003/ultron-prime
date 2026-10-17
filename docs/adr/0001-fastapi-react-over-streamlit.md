# 1. FastAPI and React over Streamlit

Status: accepted

## Context

The operator uses this from a phone as well as a desktop, needs resumable streaming, an installable app shell, accessible dialogs for consent and egress holds, and precise control of security headers.

## Decision

Backend is FastAPI, frontend is a React 18 single page app built with Vite and served by the same process.

## Consequences

More code than a Streamlit script, but the consent dialogs, hold cards, keyboard handling and offline shell are not possible to build reliably in Streamlit. One process in production keeps the run path simple.
