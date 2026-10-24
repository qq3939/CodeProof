export interface Me { id: string; login: string; name: string | null; avatar_url: string | null; role: string }

export interface Snapshot {
  id: string; repository_id: string; commit_sha: string; ref: string | null; status: string; progress: number
  message: string | null; error: string | null; size_bytes: number | null; file_count: number | null
  lang_stats: Record<string, number> | null; stats: Record<string, any> | null; base_snapshot_id: string | null
  created_at: string
}

export interface Repo {
  id: string; url: string; owner: string; name: string; default_branch: string | null; is_private: boolean
  ref: string | null; latest_snapshot: Snapshot | null
}

export interface Evidence { path: string; start_line: number; end_line: number }

export interface Topic {
  id: string; kind: string; title: string; description: string; evidence: Evidence[]; importance: number; topic_key: string
}

export interface Turn {
  seq: number; role: 'examiner' | 'user' | 'hint' | 'system'; content: string; question_id: string | null
  depth: number; meta: Record<string, any> | null; created_at: string
}

export interface SessionInfo {
  id: string; snapshot_id: string; mode: string; difficulty: string; question_count: number; max_depth: number
  status: string; total_tokens: number; cost: number; finish_reason: string | null; started_at: string
  finished_at: string | null; repo_name?: string | null
}

export interface SessionState {
  status: string; awaiting_answer: boolean; index: number; total: number; depth: number; max_depth: number
  hint_level: number; allow_hints: boolean; current_topic: string | null; current_kind: string | null
  question_id: string | null; coverage: string[]; finish_reason: string | null
}

export interface SessionDetail { session: SessionInfo; turns: Turn[]; state: SessionState | null; events: any[] }

// 通用模式（D-021）：仓库主语言没有专门的画像，考点由模型挑出，要让用户知道题目质量会低一些
export const GENERIC_MODE_NOTE =
  '这个仓库的主语言没有专门的符号索引和画像，用的是通用模式：考点由模型根据目录、README、主要源码和关键词搜索挑出，每个考点仍然校验证据行号，但题目质量比有画像的语言（Java）低。'

export const MODE_LABEL: Record<string, string> = {
  deep_dive: '项目深挖', kb_link: '八股联系项目', resume: '简历追问', chain: '链路学习', change: '改动理解',
}
export const KIND_LABEL: Record<string, string> = {
  design: '设计决策', risk: '风险点', chain: '链路', mechanism: '机制', change: '改动', resume: '简历',
}
export const STATUS_LABEL: Record<string, string> = {
  queued: '排队中', cloning: '克隆中', indexing: '建索引', profiling: '生成画像', generating: '出题中', ready: '已完成',
  failed: '失败', active: '进行中', preparing: '准备中', grading: '阅卷中', finished: '已结束', running: '运行中', done: '完成',
}
export const VERDICT_LABEL: Record<string, { text: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  hit: { text: '命中', type: 'success' }, partial: { text: '部分命中', type: 'warning' }, miss: { text: '未命中', type: 'danger' },
  supported: { text: '有证据', type: 'success' }, partial_claim: { text: '部分有', type: 'warning' },
  not_found: { text: '找不到', type: 'info' }, contradicted: { text: '与代码矛盾', type: 'danger' },
  unverifiable: { text: '代码中无法验证', type: 'info' },
}
