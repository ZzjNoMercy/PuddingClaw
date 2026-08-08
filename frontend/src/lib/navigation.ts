/**
 * Navigation configuration for PuddingClaw multi-page app.
 */

export interface NavItem {
  label: string;
  href: string;
  icon: string; // lucide icon name
  children?: NavItem[];
}

export const NAV_ITEMS: NavItem[] = [
  {
    label: "对话",
    href: "/",
    icon: "MessageSquare",
  },
  {
    label: "知识库",
    href: "/knowledge",
    icon: "Database",
  },
  {
    label: "智能问数",
    href: "/analytics",
    icon: "BarChart3",
  },
  {
    label: "扩展",
    href: "/extension/skills",
    icon: "Puzzle",
    children: [
      { label: "扩展管理", href: "/extension/skills", icon: "Settings2" },
      { label: "MCP 服务", href: "/extension/mcp", icon: "Server" },
      { label: "版本对比", href: "/extension/skills/compare", icon: "GitCompare" },
      { label: "评估审核", href: "/extension/skills/review", icon: "ClipboardCheck" },
    ],
  },
  {
    label: "评估",
    href: "/evaluation/datasets",
    icon: "FlaskConical",
  },
  {
    label: "设置",
    href: "/settings",
    icon: "Settings",
  },
];

export const ROUTE_TITLES: Record<string, string> = {
  "/": "对话",
  "/knowledge": "知识库",
  "/analytics": "智能问数",
  "/extension/skills": "扩展管理",
  "/extension/mcp": "MCP 服务",
  "/extension/skills/compare": "版本对比",
  "/extension/skills/review": "评估审核",
  "/evaluation/datasets": "评测集",
  "/evaluation/experiments": "Experiments",
  "/settings": "系统设置",
  "/settings/web-search": "联网搜索",
};
