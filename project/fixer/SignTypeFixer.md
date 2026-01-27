# solveSignedTypedConflict関数 仕様

## 概要
解析結果を入力として左辺と右辺の型に関してSignedとUnsignedを合わせて、
その結果の文字列を関数の返り値として返す関数。

## 引数
### 解析結果 analize_result
```json
{
  "analizeID": "A-004",
  "line": 31,
  "spelling": "int y = @1",
  "chenge_spelling": "@1;",
  "eval_datas": [
    {
      "eval_col": 20,
      "eval_spelling": "foo(a + b) + bar(c)",
      "eval_spelling_extend": "foo(a + b) + bar(c)",
      "operator_spelling": "+",
      "operator_cursor": "<Cursor>",
      "operator_col": 20,
      "left_val_spelling": "foo(a + b)",
      "left_val_cursor_head": "<Cursor>",
      "left_val_kind": "function",
      "left_kind_op": "foo(a, b)",
      "left_col": 9,
      "left_spel_insert_id": 1,
      "right_val_spelling": "bar(c)",
      "right_val_cursor_head": "<Cursor>",
      "right_val_kind": "function",
      "right_kind_op": "bar(c)",
      "right_col": 22,
      "right_spel_insert_id": 1
    }
  ]
}
```

#### 例
```json
{
  "analizeID": "A-0",
  "line": 106,
  "spelling": "    - (a + @1) * (a + b))",
  "chenge_spelling": "EFGHIJK * compare_and_select(a + b, EFGHIJK)",
  "eval_datas": [
    {
      "eval_col": 20,
      "eval_spelling": "5 * compare_and_select(a + b, 5)",
      "eval_spelling_extend": "EFGHIJK * compare_and_select(a + b, EFGHIJK)",
      "operator_spelling": "*",
      "operator_cursor": "<clang-cursor>",
      "operator_col": 20,
      "left_val_spelling": "EFGHIJK",
      "left_val_cursor_head": "<clang-cursor>",
      "left_val_kind": "val",
      "left_kind_op": null,
      "left_col": 12,
      "left_spel_insert_id": 1,
      "right_val_spelling": "compare_and_select(a + b, EFGHIJK)",
      "right_val_cursor_head": "<clang-cursor>",
      "right_val_kind": "val",
      "right_kind_op": null,
      "right_col": 22,
      "right_spel_insert_id": 1
    }
  ]
}
```

## 返り値
- キャスト挿入後の式の文字列  
  例: `"a = (int)a + b"` // aをintにキャスト

## ステップバイステップ仕様

### 1. 型の取得
- 左辺: `left_val_cursor_head` から変数かまたは子ノードにおいて再帰的に変数であるか探索し、変数であれば型をcursorから取得する。
- 右辺も同様に`right_val_cursor_head`から求める。
- 取得できた型を`left_val_type`, `right_val_type`とする。両方取得できなければ`return []`。

#### 型取得の具体例（clang.cindex使用例）
```python
def get_type_from_cursor(cursor):
    if cursor is None:
        return None
    if cursor.kind.is_declaration() or cursor.kind.is_reference():
        return cursor.type.spelling
    for child in cursor.get_children():
        t = get_type_from_cursor(child)
        if t:
            return t
    return None
```

### 2. プリミティブ型への正規化
- typedef等でエイリアスされている場合も、最もプリミティブな型（例: `int`, `uint8_t` など）まで掘り下げて判定する。

#### typedefチェーンの辿り方例
```python
# clang.cindex Typeオブジェクトの場合
canonical_type = cursor.type.get_canonical().spelling
# typedef名も保持したい場合は cursor.type.spelling も記録
```

### 3. 単行式・定数判定
- `left_val_spelling`/`right_val_spelling`に二項演算子が含まれていなければ単行式とみなす。
- 単行式の場合、数値リテラルかどうかで定数か変数かを判定する。
- 型がint系かどうか、unsignedかどうかを判定し、  
  - `left_val_int_bool`, `right_val_int_bool`  
  - `left_val_unsigned_bool`, `right_val_unsigned_bool`  
  を求める。
- プリミティブな型（例: 'int','unsigned int','uint8_t','uint32_t','UINT32','int32','int8_t','bool','char','float','float32','unsigned char',...）以外はプリミティブ型ではないとみなす。

#### 数値リテラル判定の正規表現例
```python
import re
def is_numeric_literal(s):
    return bool(re.fullmatch(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?[uUlLfF]*', s.strip()))
```

### 4. キャストが必要かどうかの判定
- 両辺ともint型で、unsigned/signedが不一致の場合のみキャストを行う。
- どちらか一方がint型でなければキャスト不要。
- 両方unsignedまたは両方signedの場合もキャスト不要。

#### 判定フロー
- [4.3.1] `left_val_int_bool == right_val_int_bool == TRUE` か判定。どちらか一方がFALSEならキャスト不要。
- [4.3.2] `left_val_unsigned_bool != right_val_unsigned_bool` の時のみキャスト必要。
- それ以外はキャスト不要。

### 5. キャスト対象の決定
- 型が決まっていない方を優先してキャスト対象とする（最優先）。
- どちらも決まっていない場合はキャスト対象外。
- 基本的に右辺をキャスト対象とする（優先度低）。
- 式の長さが短い方を優先してキャストする（優先度中）。
- どちらかが定数の場合は定数を優先してキャストする（優先度高）。
- キャスト型は、キャストしない側の型（typedef名含む）を使う。

### 6. キャスト式の生成
- `which` が `"left"` の場合:  
  - 単行式なら `(cast_type)left_val`
  - 単行式でなければ `(cast_type)(left_val)`
- `which` が `"right"` の場合も同様。

### 7. 式の再構成
- `eval_val = left_val + " " + operator_spelling + " " + right_val`
- `result = spelling` の `@1` を `eval_val` で置換したもの
- `result` を返す

---

## 注意点・不足点と対策

- **型取得の具体的な手順（AST API例や疑似コード）は未記載**  
  → 上記「型取得の具体例」を参照。clang.cindexのCursor/type APIを使う。

- **typedefチェーンの辿り方・正規化例は要実装**  
  → 上記「typedefチェーンの辿り方例」を参照。`get_canonical()`でプリミティブ型へ。

- **数値リテラル判定の正規表現例は要実装**  
  → 上記「数値リテラル判定の正規表現例」を参照。

- **関数呼び出しや複雑な式の型推論は制限あり**  
  → 関数呼び出しは`CALL_EXPR`の`cursor.type.spelling`で戻り値型を取得。型不明時はキャストしない・警告等。

- **eval_datasが複数ある場合の処理方針は未記載**  
  → 通常は`eval_datas[0]`のみ処理。複数対応する場合は全てに同じ処理を適用する仕様を明記。

- **返り値が `[]` の場合の呼び出し側の扱いは要検討**  
  → 呼び出し側で「型取得失敗」や「キャスト不要」として元の式をそのまま使う、または警告を出す仕様を明記。

---

## 実装時のポイント

- 型取得・正規化・キャスト判定・式再構成をそれぞれ関数化しておくと保守性が高まります。
- 仕様の各ステップを関数コメントやテストケースとして明示しておくと実装・検証が容易



