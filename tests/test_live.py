"""Penny live suite — REAL API calls, budget-capped. Tests intelligence,
comprehension, effectiveness, and per-turn cost efficiency against the actual model.

Run:  ./venv/bin/python tests/test_live.py          (~3-5 cents, isolated DB)
Refuses to run past the budget cap. Never touches the real penny.db."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import memory  # noqa: E402

memory.DB_PATH = pathlib.Path("/tmp/penny_livetest.db")  # BEFORE brain import matters not, but be safe
import brain  # noqa: E402

BUDGET = 0.06  # hard cap for one full suite run

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, condition, detail=""):
    results.append((PASS if condition else FAIL, name, detail))


def spent():
    return brain.today_spend()


def run():
    if memory.DB_PATH.exists():
        os.remove(memory.DB_PATH)
    memory.init()
    memory.set_setting("pref_emoji_level", "minimal")
    memory.set_setting("pref_reply_length", "short")
    start = spent()

    def turn(msg):
        if spent() - start > BUDGET:
            raise SystemExit(f"budget cap ${BUDGET} hit — aborting suite")
        return brain.respond(msg)

    # 1. COMPREHENSION + EFFECTIVENESS: multi-item ramble -> every item captured
    r = turn("okay brain dump: return the blue dress to nordstrom by next friday, "
             "book a hair appointment before the 20th, my rent is due on the 1st every month, "
             "and text lauren back about brunch sunday")
    items = {i["title"].lower(): i for i in memory.open_items()}
    check("capture: all 4 items saved", len(items) == 4, f"{len(items)} items")
    check("capture: recurrence detected on rent",
          any(i["recurrence"] == "monthly" for i in items.values()))
    check("capture: confirmation names items", "nordstrom" in r.lower() or "dress" in r.lower(), r[:80])

    # 2. INTELLIGENCE: general knowledge without internet
    r = turn("should i get bulk paper towels at trader joes or costco?")
    check("knowledge: answers confidently", "costco" in r.lower(), r[:80])
    check("knowledge: no 'look it up yourself'", "look it up" not in r.lower())

    # 3. RELIABILITY: completion is acknowledged by name, never bare 'Done.'
    r = turn("texted lauren back!")
    open_titles = [i["title"].lower() for i in memory.open_items()]
    check("completion: item actually closed", not any("lauren" in t for t in open_titles))
    check("completion: reply names the change", r.strip().lower() != "done.", r[:80])

    # 4. MALLEABILITY: live behavior change is persisted, not just promised
    turn("stop using emojis completely please")
    check("malleability: preference persisted", memory.get_setting("pref_emoji_level") == "none")

    # 5. COMPREHENSION: correction replaces, never argues
    memory.add_fact("Her sister's name is Kaylee")
    r = turn("my sister's name is KAYLA not kaylee, fix that")
    facts = " | ".join(f["content"] for f in memory.all_facts())
    check("correction: old fact gone", "Kaylee" not in facts, facts[:100])
    check("correction: new fact present", "Kayla" in facts)
    check("correction: no arguing", "actually" not in r.lower() and "kaylee" not in r.lower(), r[:80])

    # 6. STYLE COMPLIANCE (after the 'no emojis' instruction above)
    r = turn("what's on my list?")
    emoji_count = sum(1 for ch in r if ord(ch) > 0x1F000)
    check("style: zero emojis after 'none' pref", emoji_count == 0, f"{emoji_count} emojis")
    check("style: brief (<600 chars)", len(r) < 600, f"{len(r)} chars")

    # 7. COST EFFICIENCY
    total = spent() - start
    turns = 6
    check("cost: whole suite under budget", total <= BUDGET, f"${total:.4f}")
    check("cost: average turn under 1 cent", total / turns < 0.01, f"${total/turns:.4f}/turn")

    os.remove(memory.DB_PATH)

    print(f"\n=== Penny live suite: {sum(1 for r in results if r[0]==PASS)}/{len(results)} passed, ${total:.4f} spent ===")
    for status, name, detail in results:
        mark = "✓" if status == PASS else "✗"
        print(f"  {mark} {name}" + (f"  [{detail}]" if detail and status == FAIL else ""))
    if any(r[0] == FAIL for r in results):
        sys.exit(1)


if __name__ == "__main__":
    run()
