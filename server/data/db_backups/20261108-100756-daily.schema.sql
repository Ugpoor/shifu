CREATE TABLE accounts (
  account_id    TEXT PRIMARY KEY,
  phone         TEXT UNIQUE NOT NULL,       -- 11 位手机号，账号唯一标识
  nickname      TEXT NOT NULL,
  avatar        TEXT NOT NULL DEFAULT '🙂', -- 头像 emoji
  avatar_color  TEXT NOT NULL DEFAULT '#07c160',
  points        INTEGER NOT NULL DEFAULT 0, -- 我的积分
  last_signin   REAL NOT NULL DEFAULT 0,    -- 上次签到时间戳（用于每日签到）
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
, avatar_url TEXT, wechat_openid TEXT, wechat_unionid TEXT);
CREATE TABLE breakpoints (
  breakpoint_id TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL,
  room_id       TEXT NOT NULL,
  reason        TEXT,
  form_schema   TEXT NOT NULL,              -- JSON：title/description/fields[]
  continue_endpoint TEXT,
  cancel_endpoint   TEXT,
  status    TEXT NOT NULL,                  -- pending | answered | canceled
  answer_form_data TEXT,                    -- JSON
  answer_submit_source TEXT,                -- panel_human | chat_agent
  answered_by TEXT,
  created_at REAL NOT NULL,
  answered_at REAL
);
CREATE TABLE contacts (
  owner_id  TEXT NOT NULL,
  friend_id TEXT NOT NULL,
  alias     TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (owner_id, friend_id)
);
CREATE TABLE event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id TEXT,
  event_type TEXT NOT NULL,
  payload TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE friend_requests (
  request_id TEXT PRIMARY KEY,
  from_id    TEXT NOT NULL,
  to_id      TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted
  created_at REAL NOT NULL
);
CREATE TABLE meeting_state (
  room_id    TEXT PRIMARY KEY,
  round_no   INTEGER NOT NULL DEFAULT 0,
  started_at REAL,
  spoken     TEXT,                          -- JSON {member_id:{msg_id,at,name}}
  updated_at REAL NOT NULL
);
CREATE TABLE members (
  member_id  TEXT PRIMARY KEY,
  room_id    TEXT NOT NULL,
  name       TEXT NOT NULL,
  role       TEXT NOT NULL,                 -- host(主持人/咨询师) | expert(协作专家)
  created_at REAL NOT NULL
, account_id TEXT);
CREATE TABLE memory_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL,
  version     INTEGER NOT NULL,
  content     TEXT NOT NULL,                -- JSON：constraints/assumptions/consensus/acceptance/risks
  source      TEXT NOT NULL DEFAULT 'auto', -- auto | manual
  created_at  REAL NOT NULL
);
CREATE TABLE messages (
  msg_id     TEXT PRIMARY KEY,
  room_id    TEXT NOT NULL,
  member_id  TEXT NOT NULL,
  role       TEXT NOT NULL,                 -- host | expert | agent(表层Agent)
  content    TEXT NOT NULL,
  msg_type   TEXT NOT NULL DEFAULT 'text',  -- text | agent_confirm | system
  meta       TEXT,                          -- JSON：如填表确认留痕
  created_at REAL NOT NULL
, withdrawn INTEGER NOT NULL DEFAULT 0, reply_to TEXT, mentions TEXT, topics TEXT);
CREATE TABLE rooms (
  room_id      TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  agent_silent INTEGER NOT NULL DEFAULT 0,   -- 主持人静默开关：1=专家发言不触发表层Agent
  created_at   REAL NOT NULL
, flow_settings TEXT, theme TEXT, tags TEXT, category TEXT DEFAULT '', task_template TEXT);
CREATE TABLE sessions (
  token       TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE task_logs (
  log_id    TEXT PRIMARY KEY,
  task_id   TEXT NOT NULL,
  room_id   TEXT NOT NULL,
  event     TEXT NOT NULL,                  -- task_dispatched|breakpoint_raised|breakpoint_answered|task_resumed|task_finished|task_canceled|task_error...
  detail    TEXT,                           -- JSON
  created_at REAL NOT NULL
);
CREATE TABLE task_progress (
  progress_id TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL,
  room_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,                 -- thinking（思维链/详情）| content（实际回复/摘要）
  text       TEXT NOT NULL,
  is_read    INTEGER NOT NULL DEFAULT 0,    -- 0=未读（红点）1=已读
  created_at REAL NOT NULL
);
CREATE TABLE tasks (
  task_id     TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL,
  summary     TEXT NOT NULL,
  status      TEXT NOT NULL,                -- queued|running|waiting_human|finished|canceled|failed
  context_package TEXT,                     -- JSON：discussion_history/constraints/deliver_requirement
  result_summary  TEXT,
  result_full     TEXT,
  attachments     TEXT,                     -- JSON 数组
  error_message   TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
, closed_by TEXT, closed_at REAL, theme TEXT, agent_kind TEXT DEFAULT 'generic', result_meta TEXT, is_rolled_back INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, template TEXT, stage TEXT DEFAULT '需求整理', requirement TEXT);
