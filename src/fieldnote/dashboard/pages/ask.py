"""Ask FieldNote: chat over the local database with citations and short session memory."""

from __future__ import annotations

import streamlit as st

from fieldnote.ask.answer import AskSession, ask
from fieldnote.config import get_settings
from fieldnote.costs import CostTracker
from fieldnote.dashboard.components import DashContext, md
from fieldnote.llm.base import make_llm
from fieldnote.llm.cache import LLMCacheStore

EXAMPLES = [
    "What changed in competitor pricing this week?",
    "What are customers complaining about?",
    "Which brands gained share last month?",
    "What are our top opportunities?",
    "Which action items are open?",
]


def render(ctx: DashContext) -> None:
    key = f"ask_{ctx.cfg.workspace}"
    if key not in st.session_state:
        st.session_state[key] = {"session": AskSession(), "history": []}
    state = st.session_state[key]
    llm = make_llm(
        get_settings(),
        offline=ctx.fixture,
        cache=LLMCacheStore(ctx.db, ctx.cfg.workspace),
        cost=CostTracker(budget_usd=ctx.cfg.limits.max_llm_cost_per_run_usd),
    )
    top = st.columns([5, 1])
    top[0].caption(
        f"Answers use only the local database and cite their sources. LLM: {llm.name}. "
        "If the evidence is not there, FieldNote says what is missing."
    )
    if top[1].button("New chat", width="stretch"):
        state["session"] = AskSession()
        state["history"] = []
    chosen = st.pills("Try", EXAMPLES, selection_mode="single", key=f"{key}_ex")
    for role, text in state["history"]:
        with st.chat_message(role):
            st.markdown(md(text))
    question = st.chat_input("Ask about changes, competitors, customers, metrics, opportunities or actions")
    if not question and chosen and st.session_state.get(f"{key}_last_ex") != chosen:
        question = chosen
        st.session_state[f"{key}_last_ex"] = chosen
    if question:
        state["history"].append(("user", question))
        with st.chat_message("user"):
            st.markdown(md(question))
        with st.chat_message("assistant"), st.spinner("Searching the local database..."):
            ans = ask(ctx.db, ctx.cfg, question, llm=llm, session=state["session"], now=ctx.now)
            st.markdown(md(ans.markdown))
            st.caption(f"intent: {ans.intent} ({ans.route_via})")
        state["history"].append(("assistant", ans.markdown))
