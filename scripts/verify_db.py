"""Quick DB verification script."""
import os
import psycopg2

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM oa_master_items")
total = cur.fetchone()[0]
print(f"Total items in oa_master_items: {total}")

cur.execute("SELECT category, COUNT(*) FROM oa_master_items GROUP BY category ORDER BY COUNT(*) DESC")
rows = cur.fetchall()
print()
print(f"{'Category':<40} {'Count':>5}")
print("-" * 47)
for r in rows:
    print(f"  {r[0]:<38} {r[1]:>5}")

cur.execute("SELECT COUNT(*) FROM oa_master_synonyms")
syn_count = cur.fetchone()[0]
print(f"\nTotal synonyms: {syn_count}")

cur.execute("SELECT sno, item_name, category FROM oa_master_items ORDER BY category, sno LIMIT 20")
print("\nSample rows (first 20):")
print(f"  {'Sno':<6} {'Category':<30} {'Item Name'}")
print("  " + "-" * 65)
for r in cur.fetchall():
    print(f"  {str(r[0]):<6} {r[2]:<30} {r[1]}")

conn.close()
