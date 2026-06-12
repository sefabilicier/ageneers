# test_parse.py
import sys
sys.path.insert(0, 'D:\\ageneers')

from app.agents.codegeneer import _parse_llm_output

# Test 1: Basit JSON
print("=== Test 1: Basit JSON ===")
test1 = '[{"path": "a.py", "content": "print(\\"hello\\")"}]'
print(f"Input: {test1}")
try:
    result = _parse_llm_output(test1)
    print(f"✓ SUCCESS: {result}\n")
except Exception as e:
    print(f"✗ FAIL: {e}\n")

# Test 2: Regex içeren (sorunlu case)
print("=== Test 2: Regex içeren ===")
test2 = r'[{"path": "a.py", "content": "re.match(r\"[a-z]+@[a-z]+\\.com\", x)"}]'
print(f"Input: {test2}")
try:
    result = _parse_llm_output(test2)
    print(f"✓ SUCCESS: {result}\n")
except Exception as e:
    print(f"✗ FAIL: {e}\n")

# Test 3: Çoklu dosya
print("=== Test 3: Çoklu dosya ===")
test3 = '[{"path": "app/routes.py", "content": "from fastapi import APIRouter\\n\\nrouter = APIRouter()"}, {"path": "tests/test_routes.py", "content": "def test_route():\\n    pass"}]'
print(f"Input (ilk 100 chars): {test3[:100]}...")
try:
    result = _parse_llm_output(test3)
    print(f"✓ SUCCESS: {len(result)} files parsed")
    for r in result:
        print(f"  - {r['path']}: {len(r['content'])} chars")
    print()
except Exception as e:
    print(f"✗ FAIL: {e}\n")

print("=== Test tamamlandı ===")