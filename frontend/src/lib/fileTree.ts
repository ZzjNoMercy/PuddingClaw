export type FileTreeSource = {
  path: string;
  name?: string;
  relative_path?: string;
};

export type FileTreeNode<T extends FileTreeSource> =
  | {
      kind: "directory";
      name: string;
      path: string;
      children: FileTreeNode<T>[];
    }
  | {
      kind: "file";
      name: string;
      path: string;
      file: T;
    };

type MutableDirectory<T extends FileTreeSource> = {
  kind: "directory";
  name: string;
  path: string;
  children: Map<string, MutableNode<T>>;
};

type MutableNode<T extends FileTreeSource> =
  | MutableDirectory<T>
  | {
      kind: "file";
      name: string;
      path: string;
      file: T;
    };

function normalizedSegments(file: FileTreeSource): string[] {
  const relativePath = file.relative_path || file.name || file.path;
  return relativePath
    .replaceAll("\\", "/")
    .split("/")
    .filter((segment) => segment && segment !== ".");
}

function freezeNodes<T extends FileTreeSource>(
  nodes: Iterable<MutableNode<T>>,
): FileTreeNode<T>[] {
  return Array.from(nodes, (node) => {
    if (node.kind === "file") return node;
    return {
      kind: "directory",
      name: node.name,
      path: node.path,
      children: freezeNodes(node.children.values()),
    };
  });
}

export function buildFileTree<T extends FileTreeSource>(
  files: readonly T[],
): FileTreeNode<T>[] {
  const root = new Map<string, MutableNode<T>>();

  files.forEach((file) => {
    const segments = normalizedSegments(file);
    if (!segments.length) return;

    let children = root;
    let parentPath = "";
    segments.forEach((segment, index) => {
      const nodePath = parentPath ? `${parentPath}/${segment}` : segment;
      const isFile = index === segments.length - 1;

      if (isFile) {
        children.set(nodePath, {
          kind: "file",
          name: segment,
          path: nodePath,
          file,
        });
        return;
      }

      const existing = children.get(nodePath);
      let directory: MutableDirectory<T>;
      if (existing?.kind === "directory") {
        directory = existing;
      } else {
        directory = {
          kind: "directory",
          name: segment,
          path: nodePath,
          children: new Map(),
        };
        children.set(nodePath, directory);
      }
      children = directory.children;
      parentPath = nodePath;
    });
  });

  return freezeNodes(root.values());
}

export function fileDirectoryPaths(relativePath: string): string[] {
  const segments = relativePath
    .replaceAll("\\", "/")
    .split("/")
    .filter((segment) => segment && segment !== ".");
  return segments.slice(0, -1).map((_, index) => segments.slice(0, index + 1).join("/"));
}
