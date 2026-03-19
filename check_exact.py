import re
with open('control_plane/api/chat.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    for i, line in enumerate(lines[1447:1475], start=1448):
        print(f'{i}: {repr(line)}')