"""中文 prompt 构建器：画像字段抽取与搜索意图解析。

Phase 1 仅 MockAIProvider，不调用真实模型；接入 DeepSeek 等真 provider 时，
由 adapter 调用这里的构建器生成 prompt 文本。prompt 满足 DeepSeek JSON mode
的硬性要求（含 "json" 一词 + 给出格式示例）。
"""
