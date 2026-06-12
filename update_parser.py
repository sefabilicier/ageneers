# update_parser_fixed.py
import re

# Orijinal dosyayı oku
with open("app/agents/codegeneer.py", "r", encoding="utf-8") as f:
    content = f.read()

# Yeni _parse_llm_output fonksiyonu (raw string kullan)
new_func = r'''def _parse_llm_output(raw: str) -> list[dict[str, str]]:
    import re as _re
    raw = raw.strip()
    
    # Strip markdown fences
    raw = _re.sub(r"^```(?:json|xml)?\s*", "", raw)
    raw = _re.sub(r"\s*```$", "", raw)

    # XML format: <files><file><path>...</path><content>...</content></file></files>
    if "<file>" in raw or "<files>" in raw:
        results = []
        for block in _re.findall(r"<file>(.*?)</file>", raw, _re.DOTALL):
            path_m    = _re.search(r"<path>(.*?)</path>", block, _re.DOTALL)
            content_m = _re.search(r"<content>(.*?)</content>", block, _re.DOTALL)
            if path_m and content_m:
                p = path_m.group(1).strip()
                c = content_m.group(1)
                if c.startswith("\n"): c = c[1:]
                if c.endswith("\n"):   c = c[:-1]
                results.append({"path": p, "content": c})
        if results:
            return results

    # JSON format — with aggressive escape fix
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fix invalid escape sequences char by char
        valid = set('"' + chr(92) + "/" + "bfnrtu")
        out, i = [], 0
        while i < len(raw):
            ch = raw[i]
            if ch == chr(92) and i + 1 < len(raw) and raw[i+1] not in valid:
                out.append(chr(92))
            out.append(ch)
            i += 1
        data = json.loads("".join(out))

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    normalized = []
    for item in data:
        if isinstance(item, dict):
            path = str(item.get("path") or item.get("file_path") or "").strip()
            normalized.append({"path": path, "content": str(item.get("content", ""))})
    return normalized

'''

# Eski fonksiyonu bul ve değiştir
pattern = r'def _parse_llm_output\(raw: str\) -> list\[dict\[str, str\]\]:.*?(?=\n\ndef _validate_changes)'

try:
    new_content = re.sub(pattern, new_func, content, flags=re.DOTALL)
    
    if new_content == content:
        print("ERROR: Pattern not found - function not replaced")
        print("Checking if function exists...")
        if '_parse_llm_output' in content:
            print("Function found but pattern didn't match")
        else:
            print("Function not found in file")
    else:
        with open("app/agents/codegeneer.py", "w", encoding="utf-8") as f:
            f.write(new_content)
        print("SUCCESS: Function replaced")
except Exception as e:
    print(f"ERROR: {e}")