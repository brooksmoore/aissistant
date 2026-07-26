# Case study: building an assistant people actually trust

## The problem

Things said out loud get forgotten. "Remind me to return that package." "Dinner with her
parents is Friday." "I need to write the birthday card before Tuesday." To-do apps solve
this in theory, but they ask you to stop what you are doing, open a form, and type. In
practice the thought arrives while walking, driving, or half asleep, so it never gets
written down, and the person carries it around in their head. When the load is already
heavy, that carrying is the whole problem.

## What I built

A personal assistant that lives in Telegram, the messaging app. You talk to it, usually by
voice. It pulls every task, appointment, and fact out of what you said and saves each one.
It reminds you at the moment you asked for, keeps nudging until you tap a check-off button,
sends a short morning plan, and can edit your calendar and watch your inbox if you let it.
You change its behavior by asking in plain language: "fewer notifications" or "no evening
check-in" become real, stored settings, not polite acknowledgments.

Two people have used it every day since July 2026, on separate, fully isolated installs of
the same codebase. I built it by directing AI coding sessions rather than typing every line
myself, and I held the work to written acceptance tests with dates, metrics, and thresholds
registered before each testing window began. The judgment calls in this document, and the
decisions about what counted as done, were the part I could not delegate.

## The hard parts

**The assistant claimed it had done things it had not done.** Early on, a user asked for
fewer notifications and was told "I've turned off your evening digest." Nothing had been
turned off. The database showed no change at all. For a tool whose entire value is trust,
this was the worst possible bug. The fix was to stop trusting the model's words: after
every reply, code checks whether a claimed change has a real database action behind it. If
not, the model gets one chance to do the work or honestly walk the claim back. Later we
found the checker itself could be fooled twice in a row, so the checker got its own
checker. Every failure is counted in the database, so reliability is a number I can look
up, not an impression.

**The cost optimization I relied on had never worked.** The assistant reuses a large block
of instructions on every message, and the AI provider offers caching that makes repeated
text about 90 percent cheaper. Weeks in, I reviewed spending and found two messages sent
11 seconds apart that each cost full price. Digging in produced two findings. First, a
timestamp updated every minute sat inside the supposedly stable text, which quietly broke
the caching on every single message. Second, and bigger: the provider only caches prompts
above roughly 4,096 tokens, measured by testing progressively larger prompts, and mine was
3,716. The caching had never engaged once. Restructuring the prompt fixed both, verified
by watching the provider's own usage numbers come back. A typical day now costs a few
cents per user.

**Both bots went silent for six hours and nothing noticed.** On July 14 the processes were
running, the schedulers were ticking, but the connection to Telegram had wedged. Nothing
crashed, so nothing restarted, and no alarm existed for "alive but not actually working."
One user only found out when a message went unanswered. The fix was a small independent
watchdog that checks every five minutes whether each bot is genuinely responsive, and
alerts through the Mac's own notifications rather than through Telegram, because the
broken channel cannot be the alarm channel. Writing it surfaced its own lesson: the first
version's alerts failed silently too, which was only discovered by firing a real test
alert and getting nothing. Verification beats assumption at every layer.

## What it costs and what it does

A few cents per person per day. Two daily users. Running continuously since July 8, 2026,
with one six-hour outage, now guarded against. 242 automated tests that cost nothing to
run, plus 35 checks that run against the live AI model on a budget cap. Every bug that
reached a real user got a permanent regression test before its fix shipped.

## What I would do differently

I would design the notification system around a budget from day one. The worst week of the
project came from treating every reminder as free: one user received 25 notifications in a
day, and the trust damage took longer to repair than the code. I also patched one fragile
feature five times before accepting the design itself was wrong and rebuilding it, and the
rebuild took an afternoon. I now treat the second incident on the same feature as the
signal to stop patching. And I would have verified the caching against real usage numbers
in week one, because an optimization you have not measured is a story, not a saving.
