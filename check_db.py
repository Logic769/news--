import sqlite3
import os

conn = sqlite3.connect(os.path.expanduser(r"~\.code-review-graph\graph.db"))
cur = conn.cursor()

# Sample file paths
cur.execute("SELECT file_path FROM nodes WHERE kind='File' LIMIT 30")
paths = [r[0] for r in cur.fetchall()]
print("=== Sample file paths ===")
for p in paths[:15]:
    print(f"  {p}")

# Count by rough root (first 3 segments)
roots = {}
for p in paths:
    norm = p.replace('\\', '/')
    parts = norm.split('/')
    if len(parts) >= 3:
        root = '/'.join(parts[:3])
    else:
        root = norm
    roots[root] = roots.get(root, 0) + 1

print("\n=== Root dirs in sample ===")
for k, v in sorted(roots.items(), key=lambda x: -x[1])[:10]:
    print(f"  {v:>6}  {k}")

# Total counts
cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='File'")
total_files = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='Function'")
total_funcs = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='Class'")
total_classes = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM edges")
total_edges = cur.fetchone()[0]

print(f"\n=== Totals ===")
print(f"File nodes:     {total_files:,}")
print(f"Function nodes: {total_funcs:,}")
print(f"Class nodes:    {total_classes:,}")
print(f"Edges:          {total_edges:,}")

# Which directory has the most files?
cur.execute("SELECT file_path, COUNT(*) as cnt FROM nodes WHERE kind='File' GROUP BY file_path ORDER BY cnt DESC LIMIT 1")
r = cur.fetchone()
if r:
    print(f"\nMost common path: {r[0]} ({r[1]} nodes)")

# Project-directory files only
cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='File' AND file_path LIKE 'C:\\Users\\Logic\\Desktop\\%'")
print(f"Files under Desktop: {cur.fetchone()[0]:,}")

cur.execute("SELECT COUNT(*) FROM nodes WHERE kind='File' AND file_path LIKE 'C:\\Users\\Logic\\%'")
print(f"Files under C:\\Users\\Logic: {cur.fetchone()[0]:,}")

# Check for files outside Users/Logic
cur.execute("SELECT file_path FROM nodes WHERE kind='File' AND file_path NOT LIKE 'C:\\Users\\Logic\\%' LIMIT 10")
other = cur.fetchall()
if other:
    print("\nFiles outside C:\\Users\\Logic:")
    for r in other:
        print(f"  {r[0]}")

conn.close()
