"""内置小样本知识库（含图片 markdown），用于开箱即跑演示。

通过 POST /v1/index/sample 入库。图片走 markdown ![alt](https URL)，M5 接入多模态描述。
"""
from __future__ import annotations

from arag.schemas import IndexDocument

SAMPLE_DOCUMENTS: list[IndexDocument] = [
    IndexDocument(
        doc_id="kb-adk-overview",
        title="Google ADK 简介",
        content=(
            "# Google ADK 简介\n\n"
            "Google Agent Development Kit（ADK）是用于构建生产级 LLM 智能体的框架。\n\n"
            "核心概念包括 LlmAgent（大模型智能体）、Runner（运行时）、Tool（工具）、"
            "Session（会话）、Artifact（多模态制品）与 Plugin（生命周期插件）。\n\n"
            "ADK 通过 LiteLlm 适配层支持任意 OpenAI 兼容模型端点，例如阿里云 DashScope 的 Qwen 系列。\n"
        ),
    ),
    IndexDocument(
        doc_id="kb-rag-pipeline",
        title="混合召回 RAG 流水线",
        content=(
            "# 混合召回 RAG 流水线\n\n"
            "生产级 RAG 通常包含：文档解析、切分（chunk）、向量化（embedding）、索引、检索、重排、生成。\n\n"
            "混合召回（hybrid retrieval）同时使用向量检索与全文检索（BM25），"
            "再用互惠排名融合（RRF）合并两路结果，兼顾语义相似与关键词精确匹配。\n\n"
            "查询改写（query rewrite）会把用户问题扩展为多个等价表述以提升召回率。\n"
        ),
    ),
    IndexDocument(
        doc_id="kb-agent-loop",
        title="Agent-Loop 推理引擎",
        content=(
            "# Agent-Loop 推理引擎\n\n"
            "Agent-Loop 是 ReAct 式单循环推理引擎：模型在一个循环里反复思考、调用工具、"
            "观察结果，直到产出最终答案或计划全部完成。\n\n"
            "生产级加固包括：工具异常喂回（ToolErrorFeedback，把异常封装成 function_response 不中断对话）、"
            "上下文超长反应式截断重试（ContextOverflow）、计划续推（TaskPlanTool）。\n\n"
            "下图展示了循环的结构：\n\n"
            "![agent loop diagram](https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg)\n"
        ),
    ),
]
