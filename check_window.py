"""Quick standalone check: should the workflow proceed with a real run?

Prints exactly "true" or "false" to stdout. Kept as its own file (rather
than inline in the workflow YAML) so there's no fragile multi-line/quoted
Python embedded in the .yml - copy-pasting YAML on mobile has repeatedly
mangled quote characters, which breaks the workflow before it even starts.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("Africa/Cairo"))

weekday_ok = now.weekday() not in (4, 5)  # Friday=4, Saturday=5 are the Egypt weekend

minutes_now = now.hour * 60 + now.minute
start = 7 * 60 
end = 16 * 60
freq_end = 11 * 60 + 30

in_window = start <= minutes_now <= end
if in_window and minutes_now > freq_end:
    in_window = now.minute in (0, 15)

print("true" if (weekday_ok and in_window) else "false")
