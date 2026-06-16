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
    label: "Skills",
    href: "/skills",
    icon: "Zap",
    children: [
      { label: "配置管理", href: "/skills", icon: "Settings2" },
      { label: "版本对比", href: "/skills/compare", icon: "GitCompare" },
      { label: "评估审核", href: "/skills/review", icon: "ClipboardCheck" },
    ],
  },
  {
    label: "设置",
    href: "/settings",
    icon: "Settings",
  },
];

export const ROUTE_TITLES: Record<string, string> = {
  "/": "对话",
  "/skills": "Skills 配置",
  "/skills/compare": "版本对比",
  "/skills/review": "评估审核",
  "/settings": "系统设置",
};
