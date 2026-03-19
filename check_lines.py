with open('shared/settings_manager.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    for i, line in enumerate(lines[113:130], start=114):
        print(f'{i}: {repr(line)}')