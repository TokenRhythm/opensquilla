"""Offline orchestrator wiring tests for meta-paper-write.

Runs the default FULL_MANUSCRIPT DAG against a tmp workspace with external,
search, compile, and publish steps shimmed to deterministic fixtures. This
module verifies orchestration and delivery parsing only. The real default
four-page XeLaTeX artifact contract lives in test_meta_paper_skills.py and does
not use this compile shim.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from opensquilla.engine.types import AgentEvent, DoneEvent, TextDeltaEvent
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.events import _StepDone
from opensquilla.skills.meta.executors.agent import run_step_with_skill_stream
from opensquilla.skills.meta.executors.user_input import _render_clarify_config
from opensquilla.skills.meta.orchestrator import MetaOrchestrator
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.types import MetaMatch, MetaResult, MetaStep
from opensquilla.skills.types import SkillSpec

REPO = Path(__file__).resolve().parents[2]
BUNDLED = REPO / "src" / "opensquilla" / "skills" / "bundled"


@pytest.mark.asyncio
async def test_meta_paper_write_orchestrator_contract_wires_compile_fixture(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snap.json"
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=snapshot)
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}

    plan_spec = specs.get("meta-paper-write")
    assert plan_spec is not None, "meta-paper-write skill not bundled"
    plan = parse_meta_plan(plan_spec)
    # Pipeline rewrite: experiment/plot (skill_exec stubs) → 4 LLM
    # steps that design the experiments and render LaTeX placeholder
    # figures/tables/analysis. Plus a citation_map audit step.
    # paper_collect extracts a same-turn contract instead of pausing on a
    # form. search_query_translation then turns non-English topics into a
    # clean academic query before hitting Crossref/Brave/Tavily.
    assert plan is not None
    assert plan.final_text_mode == "step:deliver_paper"
    steps = {step.id: step for step in plan.steps}
    # paper_collect stays in the same model turn; it extracts a visible
    # contract instead of pausing on a form.
    assert steps["paper_collect"].kind == "llm_chat"
    assert steps["paper_clarify"].kind == "user_input"
    assert steps["paper_clarify"].when == (
        "'NEEDS_CLARIFICATION: yes' in outputs.paper_collect"
    )
    assert steps["paper_contract"].kind == "llm_chat"
    assert steps["paper_contract"].depends_on == ("paper_collect", "paper_clarify")
    assert steps["paper_preferences"].kind == "llm_chat"
    assert steps["paper_preferences"].depends_on == ("paper_contract",)
    assert steps["search_query_translation"].kind == "llm_chat"
    assert steps["search_query_translation"].depends_on == ("paper_contract",)
    assert steps["search_papers"].depends_on == (
        "paper_preferences", "search_query_translation",
    )
    # No more skill_exec experiment/plot stubs.
    assert "experiment" not in steps
    assert "plot" not in steps
    # New experiment design + placeholder pipeline.
    assert steps["source_readiness_gate"].kind == "skill_exec"
    assert steps["source_readiness_gate"].skill == "paper-source-readiness-gate"
    assert set(steps["source_readiness_gate"].depends_on) == {
        "paper_contract", "paper_preferences", "source_pack", "refbib",
    }
    assert steps["experiment_design"].kind == "llm_chat"
    assert steps["experiment_design"].depends_on == (
        "paper_preferences", "source_pack", "source_readiness_gate",
    )
    assert steps["figure_placeholders"].kind == "llm_chat"
    assert steps["figure_placeholders"].depends_on == ("experiment_design",)
    assert steps["table_placeholders"].kind == "llm_chat"
    assert steps["table_placeholders"].depends_on == ("experiment_design",)
    assert steps["analysis_outline"].kind == "llm_chat"
    assert set(steps["analysis_outline"].depends_on) == {
        "experiment_design", "figure_placeholders", "table_placeholders",
    }
    # Citation provenance audit is artifact-backed so full manuscript text
    # does not re-enter LLM context.
    assert steps["citation_map"].kind == "skill_exec"
    assert steps["citation_map"].skill == "paper-artifact-runtime"
    assert set(steps["citation_map"].depends_on) == {
        "length_repair_sanitizer", "refbib",
    }
    assert steps["search_papers"].kind == "skill_exec"
    assert steps["refbib"].kind == "skill_exec"
    assert "source_pack" in steps
    assert "citation_plan" in steps
    assert "final_manuscript_package" in steps
    for step_id in (
        "section_abstract",
        "section_introduction",
        "section_related_work",
        "section_method",
        "section_experiments",
        "section_discussion",
        "section_conclusion",
    ):
        assert steps[step_id].kind == "agent", step_id
        assert steps[step_id].skill == "paper-section-author", step_id
    # final_manuscript_package now also depends on the placeholder /
    # analysis blocks so they can be inlined verbatim.
    assert set(steps["final_manuscript_package"].depends_on) >= {
        "outline", "citation_plan", "refbib",
        "figure_placeholders", "table_placeholders", "analysis_outline",
    }
    assert steps["persist_sections"].kind == "skill_exec"
    assert steps["persist_sections"].skill == "paper-artifact-runtime"
    assert steps["persist_sections"].depends_on == (
        "section_abstract", "section_introduction", "section_related_work",
        "section_method", "section_experiments", "section_discussion",
        "section_conclusion",
    )
    assert steps["assemble_manuscript_tex"].depends_on == (
        "writing_plan", "persist_sections", "refbib",
    )
    assert steps["assemble_manuscript_tex"].kind == "skill_exec"
    assert steps["assemble_manuscript_tex"].skill == "paper-artifact-runtime"
    assert steps["paper_length_gate"].kind == "skill_exec"
    assert steps["paper_length_gate"].skill == "paper-length-gate"
    assert set(steps["paper_length_gate"].depends_on) == {
        "paper_contract", "length_repair_sanitizer",
    }
    assert steps["citation_integrity_gate"].kind == "skill_exec"
    assert steps["citation_integrity_gate"].skill == "paper-citation-integrity-gate"
    assert set(steps["citation_integrity_gate"].depends_on) == {
        "paper_contract", "paper_preferences", "citation_map", "paper_length_gate",
    }
    assert steps["publication_quality_gate"].kind == "skill_exec"
    assert steps["publication_quality_gate"].skill == "paper-quality-gate"
    assert set(steps["publication_quality_gate"].depends_on) >= {
        "paper_contract", "length_repair_sanitizer", "paper_length_gate",
        "citation_integrity_gate",
    }
    assert steps["latex_sanitizer"].kind == "skill_exec"
    assert steps["latex_sanitizer"].skill == "paper-latex-sanitizer"
    assert set(steps["latex_sanitizer"].depends_on) == {
        "paper_contract", "final_manuscript_package", "consistency_pass",
        "assemble_manuscript_tex",
    }
    assert "compile_latex" not in steps
    assert steps["writing_plan"].when == (
        "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
    )
    assert steps["compile_pdf"].when == (
        "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or "
        "'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
    )
    assert steps["compile_pdf"].kind == "skill_exec"
    assert steps["compile_pdf"].skill == "paper-artifact-runtime"
    assert steps["paper_length_preflight"].kind == "skill_exec"
    assert steps["precompile_length_expansion"].kind == "llm_chat"
    assert steps["compile_probe"].kind == "skill_exec"
    assert steps["page_shortfall_expansion"].kind == "llm_chat"
    assert steps["final_page_length_gate"].kind == "skill_exec"

    # Shim: replace multi-search-engine's entrypoint with a stub that
    # echoes a canned JSON. This keeps the test fully offline.
    # Use real arxiv URLs so the upgraded refbib stub emits eprint /
    # archivePrefix fields and the downstream citation_map sees a
    # STRONG source quality classification.
    stub_dir = tmp_path / "stub-search"
    stub_dir.mkdir()
    stub_script = stub_dir / "stub.py"
    stub_script.write_text(
        "import json\n"
        "# 1700.0000N is a deterministic placeholder arxiv id; the stub\n"
        "# only needs the URL pattern to match _ARXIV_RE so eprint is\n"
        "# emitted.\n"
        "results = [\n"
        "  {'title': f'Reference {i}', "
        "'url': f'https://arxiv.org/abs/1700.{i:05d}', "
        "'snippet': f'snippet {i}'}\n"
        "  for i in range(1, 26)\n"
        "]\n"
        "print(json.dumps({\n"
        "  'query': 'x',\n"
        "  'results': results,\n"
        "}))\n",
    )
    mse = specs["multi-search-engine"]
    mse.base_dir = str(stub_dir)
    mse.entrypoint = {
        "command": f"{sys.executable} {stub_script}",
        "args": [],
        "parse": "json",
        "timeout": 10,
    }
    search_results_text = (
        '{"query": "x", "results": ['
        + ",".join(
            "{"
            f'"title": "Reference {i}", '
            f'"url": "https://arxiv.org/abs/1700.{i:05d}", '
            f'"snippet": "snippet {i}"'
            "}"
            for i in range(1, 26)
        )
        + "]}"
    )
    refbib_text = "\n".join(
        "\n".join(
            [
                f"@misc{{ref{i},",
                f"  title = {{Reference {i}}},",
                f"  howpublished = {{\\url{{https://arxiv.org/abs/1700.{i:05d}}}}},",
                f"  eprint = {{1700.{i:05d}}},",
                "  archivePrefix = {arXiv},",
                "  note = {source: arxiv.org},",
                "  year = {2026}",
                "}",
            ]
        )
        for i in range(1, 26)
    )
    def long_body(label: str, start_ref: int, count: int, pages: int) -> str:
        cites = " ".join(f"\\cite{{ref{i}}}" for i in range(start_ref, start_ref + count))
        paragraph = (
            f"{label} develops the evaluation argument with concrete operational "
            f"details, explicit assumptions, comparative baselines, and deployment "
            f"constraints {cites}. The repeated offline fixture text is intentionally "
            f"long enough to exercise the long-paper compilation contract without "
            f"calling a live LLM. "
        )
        return "\n\n".join([paragraph * 8 for _ in range(pages)])

    canned_fragments: dict[str, str] = {
        "paper_preferences": (
            "PAPER_PREFERENCES:\n"
            "MODE: DIRECT\n"
            "TOPIC: RAG in low-resource settings\n"
            "AUDIENCE: academic\n"
            "VENUE_STYLE: generic research paper\n"
            "LANGUAGE: English\n"
            "TARGET_LENGTH: 10 compiled pages\n"
            "CITATION_TARGET: 20\n"
            "LENGTH_STRATEGY: allocate roughly ten compiled pages across the core sections\n"
            "CITATION_STRATEGY: use available verified sources across major claims\n"
            "DEPTH: deep\n"
            "CITATION_STYLE: numeric\n"
            "EMPHASIS:\n- reliability\n"
            "MUST_INCLUDE:\n- requested length and citation budget\n"
            "AVOID:\n- unsupported claims\n"
            "DEFAULTS_USED:\n- academic audience\n"
        ),
        "experiment_design": (
            "RESEARCH_QUESTIONS:\n"
            "  - id: RQ1; question: Does retrieval improve low-resource QA?\n"
            "  - id: RQ2; question: How does corpus size affect retrieval quality?\n"
            "  - id: RQ3; question: What are the efficiency tradeoffs?\n"
            "HYPOTHESES:\n"
            "  - id: H1; supports: RQ1; statement: RAG outperforms dense baselines.\n"
            "  - id: H2; supports: RQ2; statement: Quality plateaus past 10k docs.\n"
            "VARIABLES:\n"
            "  independent: corpus_size, retriever\n"
            "  dependent: EM, F1, latency\n"
            "  controlled: prompt, model\n"
            "DATASETS:\n"
            "  - HotpotQA-low; 1000; dev; CC BY 4.0; primary benchmark\n"
            "BASELINES:\n"
            "  - DPR; common dense retriever; ref3; ablation\n"
            "METRICS:\n"
            "  - EM; exact-match accuracy; supports: RQ1\n"
            "FIGURE_PLAN:\n"
            "  - id: fig1; type: line; x_axis: corpus size; y_axis: EM; "
            "comparison_groups: DPR / Ours; supports: RQ1; "
            "caption_hint: EM vs corpus size\n"
            "  - id: fig2; type: bar; x_axis: model; y_axis: F1; "
            "comparison_groups: 3 baselines; supports: RQ2; "
            "caption_hint: F1 by model\n"
            "TABLE_PLAN:\n"
            "  - id: tab1; columns: [Method, EM, F1, Latency]; "
            "rows_shape: 3 baselines + Ours + 1 ablation; supports: RQ1; "
            "caption_hint: main results\n"
            "ANALYSIS_DIMENSIONS:\n"
            "  - dimension: performance; figures: [fig1]; tables: [tab1]; "
            "coverage_note: headline result\n"
            "  - dimension: ablation; figures: [fig2]; tables: []; "
            "coverage_note: module contribution\n"
            "  - dimension: efficiency; figures: []; tables: [tab1]; "
            "coverage_note: latency column\n"
        ),
        "figure_placeholders": (
            "% BEGIN_FIGURE_PLACEHOLDERS\n"
            "\\begin{figure}[t]\n  \\centering\n"
            "  \\fbox{\\parbox{0.8\\linewidth}{\\textbf{[Placeholder] fig1}"
            "\\\\x: corpus size; y: EM\\\\groups: DPR / Ours\\\\supports: RQ1}}\n"
            "  \\caption{EM vs corpus size}\n  \\label{fig:fig1}\n"
            "\\end{figure}\n\n"
            "\\begin{figure}[t]\n  \\centering\n"
            "  \\fbox{\\parbox{0.8\\linewidth}{\\textbf{[Placeholder] fig2}"
            "\\\\x: model; y: F1}}\n"
            "  \\caption{F1 by model}\n  \\label{fig:fig2}\n"
            "\\end{figure}\n"
            "% END_FIGURE_PLACEHOLDERS"
        ),
        "table_placeholders": (
            "% BEGIN_TABLE_PLACEHOLDERS\n"
            "\\begin{table}[t]\n  \\centering\n"
            "  \\begin{tabular}{lccc}\n    \\toprule\n"
            "    Method & EM & F1 & Latency \\\\\n    \\midrule\n"
            "    DPR & --- & --- & --- \\\\\n"
            "    BM25 & --- & --- & --- \\\\\n"
            "    Ours & --- & --- & --- \\\\\n"
            "    Ours w/o reranker & --- & --- & --- \\\\\n"
            "    \\bottomrule\n  \\end{tabular}\n"
            "  \\caption{main results}\n  \\label{tab:tab1}\n"
            "\\end{table}\n"
            "% END_TABLE_PLACEHOLDERS"
        ),
        "analysis_outline": (
            "% BEGIN_ANALYSIS_OUTLINE\n"
            "\\subsection{Performance}\n\\label{sec:analysis-performance}\n"
            "References: \\ref{fig:fig1}, \\ref{tab:tab1}.\n"
            "Potential findings: \\begin{itemize}\\item ours wins on EM"
            "\\end{itemize}\n"
            "\\subsection{Ablation}\n"
            "References: \\ref{fig:fig2}.\n"
            "Potential findings: \\begin{itemize}\\item reranker matters"
            "\\end{itemize}\n"
            "% END_ANALYSIS_OUTLINE"
        ),
        "citation_map": (
            "CITATION_MAP:\n\n"
            "| Cite Key | Cited Times | Title | URL / DOI / arXiv | Source Quality |\n"
            "|---|---|---|---|---|\n"
            + "\n".join(
                f"| ref{i} | 1 | Reference {i} | "
                f"https://arxiv.org/abs/1700.{i:05d} (arXiv:1700.{i:05d}) | STRONG |"
                for i in range(1, 21)
            )
            + "\n\nSUMMARY: total_cite_keys=20, strong=20, ok=0, weak=0, "
            "invalid=0, unused=0"
        ),
        "source_pack": (
            "SOURCE_STATUS: sufficient\n"
            "CITATION_TARGET: 20\n"
            "USABLE_REFERENCE_COUNT: 20\n"
            "USABLE_KEYS:\n"
            + "\n".join(f"- ref{i}" for i in range(1, 21))
            + "\nEXCLUDED_KEYS:\n"
            + "\n".join(f"- ref{i} | supporting only" for i in range(21, 26))
            + "\nSOURCE_PACK:\n"
            "PRIMARY_REFERENCES:\n"
            + "\n".join(
                f"- ref{i} | Reference {i} | reliable source for claim {i}"
                for i in range(1, 21)
            )
            + "\nSUPPORTING_SOURCES:\n"
            + "\n".join(
                f"- ref{i} | Reference {i} | supporting context"
                for i in range(21, 26)
            )
            + "\nEXCLUDED_OR_WEAK_SOURCES:\nCOVERAGE_NOTES:\nCoverage is sufficient."
        ),
        "outline": (
            "ABSTRACT: This paper studies X.\n"
            "INTRODUCTION: X is important [ref1-ref6].\n"
            "METHOD: We use Y [ref7-ref12].\n"
            "RESULTS: Y improves on baseline [ref13-ref16].\n"
            "DISCUSSION: Future work [ref17-ref20]."
        ),
        "citation_plan": (
            "CITATION_PLAN:\n"
            "INTRODUCTION:\n"
            "- claim: background; cite: ref1, ref2, ref3, ref4, ref5, ref6; role: prior work\n"
            "METHOD:\n"
            "- claim: setup; cite: ref7, ref8, ref9, ref10, ref11, ref12; role: design\n"
            "RESULTS:\n"
            "- claim: comparison; cite: ref13, ref14, ref15, ref16; role: comparison\n"
            "DISCUSSION:\n"
            "- claim: implications; cite: ref17, ref18, ref19, ref20; role: limitation\n"
            "USAGE_RULES:\nUse citations only for supported claims."
        ),
        "abstract": r"\begin{abstract} This paper studies X \cite{ref1}. \end{abstract}",
        "introduction": "\\section{Introduction}\n" + long_body("Introduction", 1, 6, 3),
        "method": "\\section{Method}\n" + long_body("Method", 7, 6, 3),
        "results": (
            r"\section{Results} See Fig.~\ref{fig:1}. "
            r"\begin{figure}[t]\centering"
            r"\includegraphics[width=0.7\linewidth]{figure_1.pdf}"
            r"\caption{ours vs baseline}\label{fig:1}\end{figure}"
            + "\n"
            + long_body("Results", 13, 4, 2)
        ),
        "discussion": "\\section{Discussion}\n" + long_body("Discussion", 17, 4, 2),
    }
    manuscript_body = "\n\n".join(
        [
            canned_fragments["abstract"],
            canned_fragments["introduction"],
            "\\section{Related Work}\nRelated work fixture \\cite{ref2}.",
            canned_fragments["method"],
            canned_fragments["results"],
            canned_fragments["discussion"],
            "\\section{Conclusion}\nConclusion fixture \\cite{ref20}.",
        ],
    )
    artifact_manuscript = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"{manuscript_body}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )
    canned_fragments["final_manuscript_package"] = (
        "MANUSCRIPT_TEX:\n"
        + manuscript_body
        + "\n\nREFERENCES_BIB:\n"
        + "\n".join(f"@misc{{ref{i}, title={{Reference {i}}}}}" for i in range(1, 26))
        + "\n\nCOMPILE_NOTES:\n- figure_1.pdf provided by plot step"
    )

    async def runner(_system_prompt: str, _user_message: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(text="(unexpected agent invocation)")
        yield DoneEvent(text="")

    async def llm_chat(system_prompt: str, _user_message: str) -> str:
        if "extract paper requirements" in system_prompt:
            return (
                "TOPIC: RAG in low-resource settings\n"
                "PAPER_MODE: FULL_MANUSCRIPT\n"
                "LANGUAGE: en\n"
                "TARGET_PAGES: 10\n"
                "AUDIENCE: academic\n"
                "CITATION_TARGET: AUTO\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "SEARCH_QUERY: RAG low-resource benchmark\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n  - none\n"
                "CLARIFY_QUESTION: none\n"
                "ASSUMPTIONS:\n  - offline fixture"
            )
        if "merge extracted paper requirements" in system_prompt:
            return (
                "TOPIC: RAG in low-resource settings\n"
                "PAPER_MODE: FULL_MANUSCRIPT\n"
                "LANGUAGE: en\n"
                "TARGET_PAGES: 10\n"
                "AUDIENCE: academic\n"
                "CITATION_TARGET: AUTO\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "PDF_REQUIRED: yes\n"
                "ASSUMPTIONS:\n  - offline fixture"
            )
        if "paper requirements" in system_prompt:
            return canned_fragments["paper_preferences"]
        if "translate paper topics" in system_prompt:
            # search_query_translation stub: echo a clean English query
            # (the real LLM picks up canonical jargon; here we keep it
            # deterministic for the offline test).
            return "RAG low-resource benchmark"
        if "curate paper sources" in system_prompt:
            return canned_fragments["source_pack"]
        if "E2E search fixture" in system_prompt:
            return search_results_text
        if "E2E refbib fixture" in system_prompt:
            return refbib_text
        if "design rigorous, falsifiable experiments" in system_prompt:
            return canned_fragments["experiment_design"]
        if "placeholder figure environments" in system_prompt:
            return canned_fragments["figure_placeholders"]
        if "placeholder table environments" in system_prompt:
            return canned_fragments["table_placeholders"]
        if "analysis-chapter outlines" in system_prompt:
            return canned_fragments["analysis_outline"]
        if "long-form LaTeX paper outlines" in system_prompt:
            return canned_fragments["outline"]
        if "citation placement" in system_prompt:
            return canned_fragments["citation_plan"]
        if "writing blueprint" in system_prompt:
            return (
                "TITLE: RAG in Low-Resource Settings\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "TERMINOLOGY_LOCK: RAG, low-resource QA\n"
                "NOTATION_LOCK: use \\(q\\) for query\n"
                "PER_SECTION_BLUEPRINT:\n"
                "  abstract: {target_words: 120}\n"
                "  introduction: {target_words: 300}\n"
                "  related_work: {target_words: 200}\n"
                "  method: {target_words: 300}\n"
                "  experiments: {target_words: 300}\n"
                "  discussion: {target_words: 250}\n"
                "  conclusion: {target_words: 120}\n"
            )
        if "# paper-section-author" in system_prompt:
            if "ABSTRACT" in _user_message:
                return canned_fragments["abstract"]
            if "INTRODUCTION" in _user_message:
                return canned_fragments["introduction"]
            if "RELATED WORK" in _user_message:
                return "\\section{Related Work}\nRelated work fixture \\cite{ref2}."
            if "METHOD" in _user_message:
                return canned_fragments["method"]
            if "EXPERIMENTS" in _user_message:
                return canned_fragments["results"]
            if "DISCUSSION" in _user_message:
                return canned_fragments["discussion"]
            if "CONCLUSION" in _user_message:
                return "\\section{Conclusion}\nConclusion fixture."
            return "\\section{Section}\nFixture section."
        if "E2E assembled manuscript fixture" in system_prompt:
            paper_dir = workdir / "paper"
            paper_dir.mkdir(parents=True, exist_ok=True)
            tex_path = paper_dir / "paper.tex"
            bib_path = paper_dir / "references.bib"
            tex_path.write_text(artifact_manuscript, encoding="utf-8")
            bib_path.write_text(refbib_text, encoding="utf-8")
            return (
                f"MANUSCRIPT_PATH: {tex_path.resolve()}\n"
                f"REFERENCES_PATH: {bib_path.resolve()}\n"
                f"MANUSCRIPT_CHARS: {len(artifact_manuscript)}\n"
                "COMPILE_NOTES:\n"
                "- full manuscript persisted on disk"
            )
        if "consistency auditor" in system_prompt:
            tex_path = workdir / "paper" / "paper.tex"
            bib_path = workdir / "paper" / "references.bib"
            return (
                f"MANUSCRIPT_PATH: {tex_path.resolve()}\n"
                f"REFERENCES_PATH: {bib_path.resolve()}\n"
                "COMPILE_NOTES:\n"
                "- consistency_findings: none\n"
                "CONTEXT_POLICY: artifact-only; full manuscript omitted from prompt/output"
            )
        if "clean LaTeX manuscripts" in system_prompt:
            return canned_fragments["final_manuscript_package"]
        if "audit citation provenance" in system_prompt:
            return canned_fragments["citation_map"]
        if "manuscript length requirements" in system_prompt:
            return (
                "LENGTH_GATE: pass\nESTIMATED_WORDS: 9000\n"
                "BLOCKERS:\n  - none\nWARNINGS:\n  - none"
            )
        if "citation integrity" in system_prompt:
            return (
                "INTEGRITY: pass\nINVALID_COUNT: 0\nWEAK_PRIMARY_COUNT: 0\n"
                "UNUSED_COUNT: 0\nBLOCKERS:\n  - none\nWARNINGS:\n  - none"
            )
        if "sanitize LaTeX" in system_prompt:
            return "PASS: no markdown fences, process text, or debug logs detected"
        if "E2E deterministic LaTeX sanitizer fixture" in system_prompt:
            tex_path = workdir / "paper" / "paper.tex"
            bib_path = workdir / "paper" / "references.bib"
            return (
                "SANITIZER: pass\n"
                f"MANUSCRIPT_PATH: {tex_path.resolve()}\n"
                f"REFERENCES_PATH: {bib_path.resolve()}\n"
                "SAFE_PUNCTUATION_REPAIRS: 0\n"
                "EVIDENCE_PLACEHOLDER_REPAIRS: 0"
            )
        if "E2E publication quality gate fixture" in system_prompt:
            return "QUALITY_GATE: pass\nBLOCKERS:\n  - none"
        if "compile handoff" in system_prompt:
            return (
                "COMPILE_READY: yes\n"
                "NEXT_STEP: run latex-compile explicitly when the user asks for a PDF\n"
                "BLOCKERS:\n  - none"
            )
        if "E2E compile PDF fixture" in system_prompt:
            pdf_path = workdir / "paper" / "e2e-paper.pdf"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            document = canvas.Canvas(str(pdf_path))
            for page_number in range(1, 11):
                document.drawString(72, 720, f"E2E paper page {page_number}")
                document.showPage()
            document.save()
            page_count = len(PdfReader(pdf_path).pages)
            return (
                f"PDF_PATH: {pdf_path.resolve()}\n"
                f"PDF_PAGES: {page_count}\n"
                "PDF_TARGET_PAGES: 10\n"
                f"PDF_BYTES: {pdf_path.stat().st_size}"
            )
        if "E2E publish PDF fixture" in system_prompt:
            pdf_path = workdir / "paper" / "e2e-paper.pdf"
            assert pdf_path.is_file()
            return f"ARTIFACT_ID: paper.pdf\nPATH: {pdf_path.resolve()}"
        if "E2E persist sections fixture" in system_prompt:
            return (
                "SECTION_ARTIFACTS:\n"
                "- abstract: path=paper/sections/abstract.tex chars=120\n"
                "- introduction: path=paper/sections/introduction.tex chars=1200\n"
                "TOTAL_SECTION_CHARS: 9000\n"
                "CONTEXT_POLICY: downstream steps must read section files from disk"
            )
        if "E2E citation map fixture" in system_prompt:
            return canned_fragments["citation_map"]
        raise AssertionError(f"unexpected llm_chat prompt: {system_prompt}")

    # Each skill_exec step writes relative paths like ``paper/results.csv``;
    # they must all anchor against the same workspace so a downstream step
    # can pick up an upstream artefact. Pass ``workspace_dir`` explicitly
    # (the production runtime does the same from ``_resolve_bootstrap_workspace_dir``).
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    def replace_e2e_step(step):
        fixtures = {
            "refbib": (
                "refbib_fixture",
                "E2E refbib fixture",
                "Return the deterministic BibTeX fixture.",
            ),
            "search_papers": (
                "search_fixture",
                "E2E search fixture",
                "Return deterministic search JSON.",
            ),
            "persist_sections": (
                "persist_sections_fixture",
                "E2E persist sections fixture",
                "Return deterministic section artifact metadata.",
            ),
            "assemble_manuscript_tex": (
                "assemble_fixture",
                "E2E assembled manuscript fixture",
                "Return the deterministic manuscript package.",
            ),
            "citation_map": (
                "citation_map_fixture",
                "E2E citation map fixture",
                "Return deterministic citation audit metadata.",
            ),
            "publication_quality_gate": (
                "publication_quality_gate_fixture",
                "E2E publication quality gate fixture",
                "Return a deterministic passing publication gate.",
            ),
            "latex_sanitizer": (
                "latex_sanitizer_fixture",
                "E2E deterministic LaTeX sanitizer fixture",
                "Return the deterministic sanitized manuscript manifest.",
            ),
            "materialize_manuscript": (
                "latex_sanitizer_fixture",
                "E2E deterministic LaTeX sanitizer fixture",
                "Return the deterministic materialized manuscript manifest.",
            ),
            "paper_length_preflight": (
                "paper_length_gate_fixture",
                "E2E manuscript length requirements fixture",
                "Return a deterministic passing length preflight.",
            ),
            "length_repair_sanitizer": (
                "latex_sanitizer_fixture",
                "E2E deterministic LaTeX sanitizer fixture",
                "Return the deterministic sanitized manuscript manifest.",
            ),
            "paper_length_gate": (
                "paper_length_gate_fixture",
                "E2E manuscript length requirements fixture",
                "Return a deterministic passing length gate.",
            ),
            "compile_probe": (
                "compile_pdf_fixture",
                "E2E compile PDF fixture",
                "Return deterministic PDF compile metadata.",
            ),
            "final_latex_sanitizer": (
                "latex_sanitizer_fixture",
                "E2E deterministic LaTeX sanitizer fixture",
                "Return the deterministic final sanitized manuscript manifest.",
            ),
            "final_page_length_gate": (
                "paper_length_gate_fixture",
                "E2E manuscript length requirements fixture",
                "Return a deterministic passing final length gate.",
            ),
            "final_publication_quality_gate": (
                "publication_quality_gate_fixture",
                "E2E publication quality gate fixture",
                "Return a deterministic passing final publication gate.",
            ),
            "compile_pdf": (
                "compile_pdf_fixture",
                "E2E compile PDF fixture",
                "Return deterministic PDF compile metadata.",
            ),
            "publish_pdf": (
                "publish_pdf_fixture",
                "E2E publish PDF fixture",
                "Return deterministic artifact metadata.",
            ),
        }
        if step.id not in fixtures:
            return step
        skill_name, system_prompt, task = fixtures[step.id]
        return replace(
            step,
            kind="llm_chat",
            skill=skill_name,
            with_args={"system": system_prompt, "task": task},
        )

    run_plan = replace(
        plan,
        steps=tuple(replace_e2e_step(step) for step in plan.steps),
    )
    orch = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_PatchedLoader(loader, specs),
        workspace_dir=str(workdir),
        llm_chat=llm_chat,
    )
    final: MetaResult | None = None
    async for ev in orch.iter_events(
        MetaMatch(
            plan=run_plan,
            inputs={
                "user_message": "RAG in low-resource settings",
            },
        ),
    ):
        if isinstance(ev, MetaResult):
            final = ev

    assert final is not None
    assert final.ok, final.error
    pdf_path = workdir / "paper" / "e2e-paper.pdf"
    reader = PdfReader(pdf_path)
    assert len(reader.pages) == 10
    assert "E2E paper page 1" in (reader.pages[0].extract_text() or "")
    assert "E2E paper page 10" in (reader.pages[-1].extract_text() or "")
    assert f"PDF: {pdf_path.resolve()}" in final.final_text
    assert "Pages: 10 (target: at least 10)" in final.final_text
    assert (
        "Citations: cited keys 20; strong 20; acceptable 0; weak 0; "
        "invalid 0; unused entries 0"
    ) in final.final_text
    assert "Warnings: none" in final.final_text
    assert "COMPILE_READY" not in final.final_text
    assert f"PDF_PATH: {pdf_path.resolve()}" in final.step_outputs["compile_pdf"]
    assert "PDF_PAGES: 10" in final.step_outputs["compile_pdf"]
    assert any(
        marker in final.step_outputs["paper_length_gate"]
        for marker in ("LENGTH_GATE: pass", "LENGTH_GATE: warn")
    )
    assert "INTEGRITY: pass" in final.step_outputs["citation_integrity_gate"]
    assert "ARTIFACT_ID: paper.pdf" in final.step_outputs["publish_pdf"]
    bib_text = final.step_outputs["refbib"]
    assert "@misc{ref1," in bib_text
    # Upgraded refbib stub: arxiv URLs → eprint + source domain tag.
    assert "eprint = {1700.00001}" in bib_text
    assert "archivePrefix = {arXiv}" in bib_text
    assert "source: arxiv.org" in bib_text
    # The placeholder/analysis blocks were inlined verbatim into
    # the final manuscript so users see them in the deliverable.
    assert "BEGIN_FIGURE_PLACEHOLDERS" in final.step_outputs["figure_placeholders"]
    assert "BEGIN_TABLE_PLACEHOLDERS" in final.step_outputs["table_placeholders"]
    assert "BEGIN_ANALYSIS_OUTLINE" in final.step_outputs["analysis_outline"]
    # Citation provenance audit ran and produced a markdown table.
    assert "CITATION_MAP:" in final.step_outputs["citation_map"]
    assert "STRONG" in final.step_outputs["citation_map"]
    # No more results.csv / figure_1.pdf artefacts — the placeholder
    # pipeline is purely LaTeX.


@pytest.mark.asyncio
async def test_meta_paper_stops_before_drafting_with_three_of_fifteen_sources(
    tmp_path: Path,
) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    specs = {spec.name: spec for spec in loader.load_all()}
    plan_spec = specs.get("meta-paper-write")
    assert plan_spec is not None
    plan = parse_meta_plan(plan_spec)
    assert plan is not None

    fixture_steps = {
        "search_papers": (
            "early_gate_search_fixture",
            "Early source gate search fixture",
            "Return three deterministic results.",
        ),
        "refbib": (
            "early_gate_refbib_fixture",
            "Early source gate bibliography fixture",
            "Return three deterministic BibTeX entries.",
        ),
    }

    def replace_fixture_step(step: MetaStep) -> MetaStep:
        fixture = fixture_steps.get(step.id)
        if fixture is None:
            return step
        skill_name, system_prompt, task = fixture
        return replace(
            step,
            kind="llm_chat",
            skill=skill_name,
            with_args={"system": system_prompt, "task": task},
        )

    run_plan = replace(plan, steps=tuple(replace_fixture_step(step) for step in plan.steps))
    seen_prompts: list[str] = []

    async def runner(_system_prompt: str, _user_message: str) -> AsyncIterator[AgentEvent]:
        raise AssertionError("section author must not run after source readiness blocks")
        yield DoneEvent(text="")  # pragma: no cover - keeps this an async generator

    async def llm_chat(system_prompt: str, _user_message: str) -> str:
        seen_prompts.append(system_prompt)
        if "extract paper requirements" in system_prompt:
            return (
                "TOPIC: Synthetic edge routing\n"
                "PAPER_MODE: FULL_MANUSCRIPT\n"
                "LANGUAGE: zh\n"
                "TARGET_PAGES: 8\n"
                "AUDIENCE: academic\n"
                "CITATION_TARGET: 15\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "NEEDS_CLARIFICATION: no\n"
                "MISSING_FIELDS:\n- none\n"
                "CLARIFY_QUESTION: none\n"
                "ASSUMPTIONS:\n- offline fixture"
            )
        if "merge extracted paper requirements" in system_prompt:
            return (
                "TOPIC: Synthetic edge routing\n"
                "PAPER_MODE: FULL_MANUSCRIPT\n"
                "LANGUAGE: zh\n"
                "TARGET_PAGES: 8\n"
                "AUDIENCE: academic\n"
                "CITATION_TARGET: 15\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "PDF_REQUIRED: yes\n"
                "ASSUMPTIONS:\n- offline fixture"
            )
        if "translate paper topics" in system_prompt:
            return "multi-agent edge task routing"
        if "paper requirements" in system_prompt:
            return (
                "PAPER_MODE: FULL_MANUSCRIPT\n"
                "MODE: DIRECT\n"
                "TOPIC: Synthetic edge routing\n"
                "AUDIENCE: academic\n"
                "VENUE_STYLE: generic research paper\n"
                "LANGUAGE: zh\n"
                "TARGET_LENGTH: 8 compiled pages\n"
                "CITATION_TARGET: 15\n"
                "EVIDENCE_STATUS: not_supplied\n"
                "LENGTH_STRATEGY: eight pages\n"
                "CITATION_STRATEGY: use fifteen verified sources\n"
                "CITATION_STYLE: BibTeX cite keys, LaTeX citations\n"
                "ASSUMPTIONS:\n- none"
            )
        if "Early source gate search fixture" in system_prompt:
            return (
                '{"query":"x","results":['
                '{"title":"A","url":"https://example.test/a"},'
                '{"title":"B","url":"https://example.test/b"},'
                '{"title":"C","url":"https://example.test/c"}]}'
            )
        if "Early source gate bibliography fixture" in system_prompt:
            return "\n".join(
                f"@article{{ref{i}, title={{Synthetic {i}}}, "
                f"url={{https://example.test/{i}}}}}"
                for i in range(1, 4)
            )
        if "curate paper sources" in system_prompt:
            return (
                "SOURCE_STATUS: insufficient\n"
                "CITATION_TARGET: 15\n"
                "USABLE_REFERENCE_COUNT: 3\n"
                "USABLE_KEYS:\n- ref1\n- ref2\n- ref3\n"
                "EXCLUDED_KEYS:\n- none\n"
                "SOURCE_PACK:\n"
                "PRIMARY_REFERENCES:\n"
                "- ref1 | Synthetic 1 | background\n"
                "- ref2 | Synthetic 2 | method\n"
                "- ref3 | Synthetic 3 | evaluation\n"
                "COVERAGE_GAPS:\n- twelve references missing"
            )
        raise AssertionError(f"downstream prompt ran after early source block: {system_prompt}")

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    orchestrator = MetaOrchestrator(
        agent_runner=runner,
        skill_loader=_PatchedLoader(loader, specs),
        workspace_dir=str(workdir),
        llm_chat=llm_chat,
    )
    final: MetaResult | None = None
    async for event in orchestrator.iter_events(
        MetaMatch(
            plan=run_plan,
            inputs={"user_message": "写8页论文，至少15篇可核验参考文献"},
        ),
    ):
        if isinstance(event, MetaResult):
            final = event

    assert final is not None
    assert not final.ok
    assert final.failed_step_id == "source_readiness_gate"
    assert "found 3/15 usable references" in (final.error or "")
    assert "experiment_design" not in final.step_outputs
    assert not any("design rigorous, falsifiable experiments" in prompt for prompt in seen_prompts)
    assert not (workdir / "paper" / "paper.pdf").exists()


def test_meta_paper_clarify_copy_prefers_user_language_hint(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}
    plan_spec = specs.get("meta-paper-write")
    assert plan_spec is not None
    plan = parse_meta_plan(plan_spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}
    clarify_cfg = steps["paper_clarify"].clarify_config
    assert clarify_cfg is not None
    citation_field = next(field for field in clarify_cfg.fields if field.name == "citation_target")
    assert citation_field.type == "int"
    assert citation_field.required is False
    assert citation_field.min == 1
    assert citation_field.max == 100

    rendered_en = _render_clarify_config(
        clarify_cfg,
        inputs={
            "user_message": "Write a paper. Please ask me for the topic first.",
            "user_language": "en",
            "collected": {},
        },
        outputs={"paper_collect": "LANGUAGE: zh\nNEEDS_CLARIFICATION: yes"},
    )
    assert "Some paper details are missing" in rendered_en.intro
    assert rendered_en.fields[0].prompt == "Paper topic"
    rendered_en_fields = {field.name: field for field in rendered_en.fields}
    assert rendered_en_fields["citation_target"].prompt == (
        "Minimum verifiable references (optional)"
    )

    rendered_zh = _render_clarify_config(
        clarify_cfg,
        inputs={
            "user_message": "帮我写一篇论文，先问我主题",
            "user_language": "zh",
            "collected": {},
        },
        outputs={"paper_collect": "LANGUAGE: en\nNEEDS_CLARIFICATION: yes"},
    )
    assert "论文信息还不完整" in rendered_zh.intro
    assert rendered_zh.fields[0].prompt == "论文主题"
    rendered_zh_fields = {field.name: field for field in rendered_zh.fields}
    assert rendered_zh_fields["citation_target"].prompt == "最少可核验参考文献数量（可选）"


def test_meta_paper_delivery_is_deterministic_and_language_gated(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    specs = {s.name: s for s in loader.load_all()}
    plan_spec = specs.get("meta-paper-write")
    assert plan_spec is not None
    plan = parse_meta_plan(plan_spec)
    assert plan is not None
    steps = {step.id: step for step in plan.steps}
    deliver = steps["deliver_paper"]
    payload = str((deliver.with_args or {}).get("payload"))
    assert deliver.kind == "skill_exec"
    assert deliver.skill == "paper-delivery-summary"
    assert "outputs.paper_contract" in payload
    assert "inputs.get('language_instruction'" in payload
    assert "outputs.compile_pdf" in payload
    assert "outputs.citation_map" in payload
    assert "inputs.user_message" not in payload


@pytest.mark.asyncio
async def test_paper_section_author_step_output_uses_latex_fragment_only(
    tmp_path: Path,
) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()
    step = MetaStep(
        id="draft_results",
        skill="paper-section-author",
        kind="agent",
        with_args={"section": "results"},
    )

    async def runner(_system_prompt: str, _user_message: str) -> AsyncIterator[AgentEvent]:
        yield TextDeltaEvent(
            text=(
                "The word count is low. Let me expand it.\n"
                "```latex\n"
                "\\section{Results}\n"
                "Clean result prose with Fig.~\\ref{fig:1}.\n"
                "```\n"
                "File written to: /tmp/results.tex"
            ),
        )
        yield DoneEvent(text="")

    events = [
        ev
        async for ev in run_step_with_skill_stream(
            step,
            "paper-section-author",
            {"user_message": "topic"},
            {},
            agent_runner=runner,
            skill_loader=loader,
        )
    ]
    done = [ev for ev in events if isinstance(ev, _StepDone)]
    assert len(done) == 1
    assert done[0].text == (
        "\\section{Results}\n"
        "Clean result prose with Fig.~\\ref{fig:1}."
    )


class _PatchedLoader:
    """Wrap a SkillLoader and return the patched specs by name."""

    def __init__(self, real: SkillLoader, specs: dict[str, SkillSpec]) -> None:
        self._real = real
        self._specs = specs

    def get_by_name(self, name: str) -> SkillSpec | None:
        return self._specs.get(name) or self._real.get_by_name(name)
