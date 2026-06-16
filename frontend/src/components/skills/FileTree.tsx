"use client";

import { useState } from "react";
import { ChevronRight, ChevronDown, File, Folder, FolderOpen } from "lucide-react";
import type { FileTreeNode } from "@/lib/skillsApi";

interface FileTreeProps {
  nodes: FileTreeNode[];
  activeFile: string;
  onFileSelect: (path: string) => void;
}

export default function FileTree({ nodes, activeFile, onFileSelect }: FileTreeProps) {
  return (
    <div className="space-y-0.5">
      {nodes.map((node) => (
        <TreeNode
          key={node.path}
          node={node}
          activeFile={activeFile}
          onFileSelect={onFileSelect}
          level={0}
        />
      ))}
    </div>
  );
}

interface TreeNodeProps {
  node: FileTreeNode;
  activeFile: string;
  onFileSelect: (path: string) => void;
  level: number;
}

function TreeNode({ node, activeFile, onFileSelect, level }: TreeNodeProps) {
  const [isExpanded, setIsExpanded] = useState(level === 0); // Auto-expand root level
  const isDirectory = node.type === "directory";
  const isActive = !isDirectory && node.path === activeFile;

  const handleClick = () => {
    if (isDirectory) {
      setIsExpanded(!isExpanded);
    } else {
      onFileSelect(node.path);
    }
  };

  // Extract display name from path
  const getDisplayName = (path: string) => {
    // Remove trailing slash for directories
    const cleanPath = path.endsWith("/") ? path.slice(0, -1) : path;
    const parts = cleanPath.split("/");
    return parts[parts.length - 1] || cleanPath;
  };

  const paddingLeft = level * 12 + 8;

  return (
    <div>
      <div
        onClick={handleClick}
        className={`flex items-center gap-1.5 py-1 px-2 rounded-md cursor-pointer transition-colors ${
          isActive
            ? "bg-amber-500/10 text-amber-700"
            : "hover:bg-black/[0.04] text-gray-600"
        }`}
        style={{ paddingLeft: `${paddingLeft}px` }}
      >
        {isDirectory ? (
          <>
            {isExpanded ? (
              <ChevronDown className="w-3 h-3 text-gray-400 shrink-0" />
            ) : (
              <ChevronRight className="w-3 h-3 text-gray-400 shrink-0" />
            )}
            {isExpanded ? (
              <FolderOpen className="w-3.5 h-3.5 text-amber-500 shrink-0" />
            ) : (
              <Folder className="w-3.5 h-3.5 text-gray-400 shrink-0" />
            )}
          </>
        ) : (
          <>
            <div className="w-3 h-3 shrink-0" />
            <File className="w-3.5 h-3.5 text-gray-400 shrink-0" />
          </>
        )}
        <span className="text-[11px] truncate flex-1">
          {getDisplayName(node.path)}
        </span>
        {!isDirectory && node.size !== undefined && (
          <span className="text-[9px] text-gray-400 shrink-0">
            {formatFileSize(node.size)}
          </span>
        )}
      </div>

      {isDirectory && isExpanded && node.children && (
        <div>
          {node.children.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              activeFile={activeFile}
              onFileSelect={onFileSelect}
              level={level + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}
