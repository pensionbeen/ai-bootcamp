"""Strict, local Korean won literals; no global numeric-token expansion."""
import ast
from decimal import Decimal
import re

_NUMBER = r"(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)(?:\.\d+)?"
_COMPONENT = re.compile(r"(" + _NUMBER + r")([천백십]?)")
_BODY = r"[\d.,]+(?:[ \t]*[십백천만억조][ \t]*[\d.,]*)*"
_BOUNDARY = r"(?<![\dA-Za-z_.,십백천만억조])"
_CANDIDATE = re.compile(_BOUNDARY + "(" + _BODY + r")[ \t]*원+")
_RANGE = re.compile(_BOUNDARY + "(" + _BODY + r")[ \t]*(원)?[ \t]*[~∼–][ \t]*(" + _BODY + r")[ \t]*원(?![\d원])")
_MONEY_LABEL = r"(?:지원액|지원금|금액|수수료|사용료|이용료|요금|대관료|보증금|임대료|임차료|연회비|비용|회비|부담금|납부액|징수액)"
_TABLE_UNIT = re.compile(r"단위[ \t]*[:：][ \t]*(백만원|천원|만원|억원|원)")
_PLAIN_CELL = re.compile(r"(" + _NUMBER + r")(?:[ \t]*[~∼–][ \t]*(" + _NUMBER + r"))?")
# A field label carrying its own money unit annotation, e.g. "금 액(원): 3,000~5,000".
_HEADER_UNIT = re.compile(r"\(\s*(?:단위\s*[:：]\s*)?(백만원|천원|만원|억원|원)\s*\)")
_FIELD = re.compile(r"(?P<label>[^:：\r\n]+?)[ \t]*[:：][ \t]*(?P<value>[^\r\n]*?)[ \t]*")
# Non-currency units written directly after a number ("3시간", "2,000㎡").
_UNIT = r"(?:[가-힣]+|㎡|㎢|㎥|㎞|㎏|m²|km|kg)"
# A number written with 만/천/억 groups and no 원 ("40만㎡").
_SCALED_BODY = r"\d[\d.,]*(?:[ \t]*[십백천만억][ \t]*[\d.,]*)*[십백천만억][\d.,]*"
_SCALED = re.compile(_BOUNDARY + "(" + _SCALED_BODY + r")(?![\d원])")


def _small(text):
    if not text:
        return Decimal(0)
    total, previous, end = Decimal(0), 10000, 0
    matches = list(_COMPONENT.finditer(text))
    for index, match in enumerate(matches):
        if match.start() != end:
            raise ValueError("invalid_won_literal")
        value = Decimal(match[1].replace(",", ""))
        scale = {"천": 1000, "백": 100, "십": 10, "": 1}[match[2]]
        if scale >= previous or (scale != 1 and value >= 10):
            raise ValueError("invalid_won_literal")
        if len(matches) > 1 and (value >= previous / scale or value != value.to_integral_value()):
            raise ValueError("invalid_won_literal")
        total += value * scale
        previous, end = scale, match.end()
    if end != len(text) or not matches:
        raise ValueError("invalid_won_literal")
    return total


def parse_won(text):
    """Parse explicit descending 억/만 groups and descending 천/백/십 tails."""
    text = re.sub(r"[ \t]+", "", text)
    if not text or any(text.count(unit) > 1 for unit in "억만") or "조" in text:
        raise ValueError("invalid_won_literal")
    total, remaining, previous = Decimal(0), text, Decimal("Infinity")
    for unit, scale in (("억", 100000000), ("만", 10000)):
        if unit not in remaining:
            continue
        group, remaining = remaining.split(unit)
        value = _small(group)
        if not group or value >= 10000 or (remaining and value != value.to_integral_value()):
            raise ValueError("invalid_won_literal")
        if Decimal(scale) >= previous:
            raise ValueError("invalid_won_literal")
        total += value * scale
        previous = Decimal(scale)
    tail = _small(remaining)
    if previous != Decimal("Infinity") and tail >= previous:
        raise ValueError("invalid_won_literal")
    total += tail
    if not total.is_finite() or total > Decimal("1e15"):
        raise ValueError("invalid_won_literal")
    return str(total.normalize())


def won_literals(text, *, expression=False):
    literals = []
    def append(start, end, value):
        literals.append({"literal": text[start:end], "start": start, "end": end, "value": value})

    if not expression:
        for match in _RANGE.finditer(text):
            try:
                # A missing lower 원 is unambiguous only for plain won numbers.
                if not match[2] and re.search(r"[십백천만억조]", match[1] + match[3]):
                    raise ValueError("ambiguous_won_range")
                if match.start() and text[match.start() - 1] in "+-−":
                    raise ValueError("signed_won_literal_not_supported")
                lower, upper = parse_won(match[1]), parse_won(match[3])
                if Decimal(lower) > Decimal(upper):
                    raise ValueError("reversed_won_range")
                append(match.start(1), match.end(2) if match[2] else match.end(1), lower)
                append(match.start(3), match.end(), upper)
            except ValueError:
                append(match.start(), match.end(), None)
    for match in _CANDIDATE.finditer(text):
        if any(match.start() < literal["end"] and match.end() > literal["start"] for literal in literals):
            continue
        try:
            if match[0].endswith("원원") or (match.end() < len(text) and text[match.end()].isdigit()):
                raise ValueError("invalid_won_literal")
            if not expression and match.start() and text[match.start() - 1] in "+-−":
                raise ValueError("signed_won_literal_not_supported")
            value = parse_won(match[1])
        except ValueError:
            value = None
        append(match.start(), match.end(), value)
    if not expression:
        for literal in _table_amounts(text) + _header_unit_amounts(text):
            if not any(literal["start"] < old["end"] and literal["end"] > old["start"] for old in literals):
                literals.append(literal)
    return sorted(literals, key=lambda literal: literal["start"])


def _header_unit_amounts(text):
    """A "label: value" field whose label declares one money unit ("금 액(원): 3,000~5,000").

    Only a whole plain number or plain range value inherits the label's unit. A
    label naming two different units, or a value with any other text, is left
    alone (its numbers keep their legacy presence-only meaning).
    """
    result, cursor = [], 0
    for line in text.splitlines(keepends=True):
        field = _FIELD.fullmatch(line.rstrip("\r\n"))
        units = set(_HEADER_UNIT.findall(field["label"])) if field else set()
        matched = _PLAIN_CELL.fullmatch(field["value"]) if len(units) == 1 else None
        if matched:
            unit = Decimal(parse_won("1" + next(iter(units))[:-1]))
            for group in (1, 2):
                if matched[group] is not None:
                    start = cursor + field.start("value") + matched.start(group)
                    result.append({"literal": matched[group], "start": start, "end": start + len(matched[group]),
                                   "value": str((Decimal(matched[group].replace(",", "")) * unit).normalize()),
                                   "conversion": "explicit_column_header_unit"})
        cursor += len(line)
    return result


def scaled_quantities(text):
    """Non-currency numbers written with 만/천/억 groups ("40만㎡"), parsed by the won-literal rules.

    Spans that are won literals are skipped (they are amounts, not scalars).
    Invalid groupings are ignored, never repaired.
    """
    taken = [(literal["start"], literal["end"]) for literal in won_literals(text)]
    values = []
    for match in _SCALED.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in taken):
            continue
        try:
            values.append({"literal": match[1], "start": match.start(1), "end": match.end(1),
                           "value": parse_won(match[1]), "conversion": "explicit_scaled_quantity"})
        except ValueError:
            continue
    return values


def _table_amounts(text):
    """Only explicit monetary rows/columns inherit a nearby declared unit."""
    unit, budget, columns, width, cursor = None, 0, [], 0, 0
    result = []
    for line in text.splitlines(keepends=True):
        header = _TABLE_UNIT.search(line)
        if header:
            unit = Decimal(parse_won("1" + header[1][:-1]))
            budget, columns = 12, []
        elif budget:
            budget -= 1
        else:
            unit, columns = None, []
        if unit is not None:
            stripped = line.strip()
            cells = list(re.finditer(r"(?<=\|)[^|]*(?=\|)", line.rstrip("\r\n"))) if stripped.startswith("|") and stripped.endswith("|") else []
            if not cells:
                columns, width = [], 0
            if cells and any(re.fullmatch(_MONEY_LABEL, cell[0].strip()) for cell in cells):
                columns = [index for index, cell in enumerate(cells)
                           if re.fullmatch(_MONEY_LABEL, cell[0].strip())]
                width = len(cells)
            elif cells and (len(cells) != width or any(cell[0].strip() in ("구분", "인원", "순번", "연도", "대상") for cell in cells)):
                columns, width = [], 0
            candidates = []
            # A labelled scalar row has one and only one amount/range field.
            row = re.fullmatch(r"[ \t]*" + _MONEY_LABEL + r"[ \t:：]+(?P<value>.+?)[ \t\r\n]*", line)
            if row:
                candidates.append((row["value"], cursor + row.start("value")))
            for index in columns:
                if index < len(cells):
                    cell = cells[index]
                    value = cell[0].strip()
                    candidates.append((value, cursor + cell.start() + len(cell[0]) - len(cell[0].lstrip())))
            for value, offset in candidates:
                matched = _PLAIN_CELL.fullmatch(value)
                if matched:
                    for group in (1, 2):
                        if matched[group] is not None:
                            canonical = str((Decimal(matched[group].replace(",", "")) * unit).normalize())
                            result.append({"literal": matched[group], "start": offset + matched.start(group),
                                           "end": offset + matched.end(group), "value": canonical,
                                           "conversion": "explicit_monetary_table_unit"})
            if not header and not cells and not row:
                unit, budget, columns, width = None, 0, [], 0
        cursor += len(line)
    return result


def without_won(text, *, scaled_only=False):
    chars = list(text)
    for literal in won_literals(text):
        if scaled_only and literal["value"] is not None and not re.search(r"[십백천만억조]", literal["literal"]):
            continue  # Preserve legacy plain-number presence, including table cells.
        chars[literal["start"]:literal["end"]] = " " * (literal["end"] - literal["start"])
    return "".join(chars)


def normalize_expression(expression):
    """Replace complete currency literals only, preserving the arithmetic grammar."""
    return _normalize_with_units(expression)[0]


def _normalize_with_units(expression):
    """normalize_expression plus the unit written after each numeric constant, in source order."""
    if re.search(r"÷\s*[÷/]|/\s*÷", expression):
        raise SyntaxError("invalid_division_operator")
    literals = won_literals(expression, expression=True)
    for literal in reversed(literals):
        if literal["value"] is None:
            raise ValueError("invalid_won_literal")
        value = format(Decimal(literal["value"]), "f")
        expression = expression[:literal["start"]] + value + expression[literal["end"]:]
    # Bare comma-grouped integers are allowed only outside parentheses. Inside
    # parentheses a comma could be tuple syntax or a function-argument separator.
    # Reject all unmatched commas rather than repairing malformed grouping.
    grouped = re.compile(r"(?<![\d.,A-Za-z_])([1-9][0-9]{0,2}(?:,[0-9]{3})+)(?![\d.,A-Za-z_])")
    def ungroup(match):
        prefix = expression[:match.start()]
        if prefix.count("(") != prefix.count(")"):
            raise SyntaxError("ambiguous_grouped_calculation_literal")
        return match[1].replace(",", "")
    expression = grouped.sub(ungroup, expression)
    if "," in expression:
        raise SyntaxError("invalid_grouped_calculation_literal")
    expression, units = _quantities(expression)
    return expression.replace("×", "*").replace("÷", "/"), units


def _quantities(expression):
    """Rewrite quantity notation into plain constants and record each constant's written unit.

    "A분의 B" -> (B/A); "N%"/"N퍼센트" -> N/100; "40만㎡" -> 400000 (unit ㎡);
    "3시간" -> 3 (unit 시간). Every other number keeps its text and has no unit.
    """
    number = r"\d+(?:\.\d+)?"
    tokens = []

    def hold(text, unit=None):
        tokens.append((text, unit))
        return "\x00" + str(len(tokens) - 1) + "\x00"

    def scaled(match):
        try:
            value = format(Decimal(parse_won(match["body"])), "f")
        except ValueError:
            raise SyntaxError("invalid_scaled_quantity") from None
        return hold(value, match["unit"])

    expression = re.sub(r"(?<![\d.])(" + number + r")[ \t]*분의[ \t]*(" + number + r")(?![\d.])",
                        lambda match: "(" + hold(match[2]) + "/" + hold(match[1]) + ")", expression)
    expression = re.sub(r"(?<![\d.\x00])(" + number + r")[ \t]*(?:%|퍼센트)",
                        lambda match: hold(format((Decimal(match[1]) / 100).normalize(), "f")), expression)
    expression = re.sub(r"(?<![\d.\x00])(?P<body>" + _SCALED_BODY + r")(?:[ \t]*(?P<unit>" + _UNIT + r"))?",
                        scaled, expression)
    expression = re.sub(r"(?<![\d.A-Za-z_\x00])(" + number + r")(?![\d.\x00])(?:[ \t]*(" + _UNIT + r"))?",
                        lambda match: hold(match[1], match[2]), expression)
    units = []

    def restore(match):
        text, unit = tokens[int(match[1])]
        units.append(unit)
        return text

    return re.sub("\x00(\\d+)\x00", restore, expression), units


def expression_unit(expression):
    """Unit of an expression's value from the units written after its constants.

    Add/subtract need the same unit (or none); multiplication allows at most one
    unit-bearing factor; division by a unitless number keeps the unit and
    division by the same unit cancels it. Anything else raises ValueError.
    """
    normalized, units = _normalize_with_units(expression)
    order = iter(units)

    def unit(node):
        if isinstance(node, ast.Constant):
            return next(order)
        if isinstance(node, ast.UnaryOp):
            return unit(node.operand)
        if isinstance(node, ast.Call) and len(node.args) == 1:
            return unit(node.args[0])
        if not isinstance(node, ast.BinOp):
            raise ValueError("unsupported_unit_expression")
        left, right = unit(node.left), unit(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mod)) and left == right:
            return left
        if isinstance(node.op, ast.Mult) and (left is None or right is None):
            return left or right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right is None:
            return left
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and left == right:
            return None
        raise ValueError("incompatible_quantity_units")

    try:
        result = unit(ast.parse(normalized, mode="eval").body)
    except StopIteration:
        raise ValueError("unaligned_quantity_units") from None
    if next(order, None) is not None or len(units) != sum(
            isinstance(node, ast.Constant) for node in ast.walk(ast.parse(normalized, mode="eval"))):
        raise ValueError("unaligned_quantity_units")
    return result


def declared_quantity(result):
    """(value, unit) of a declared calculation result: one number with an optional written unit.

    "2,400" -> ("2400", None); "2시간" -> ("2", "시간"); "8,000원" -> ("8000", "원").
    Anything else (symbolic text, several numbers) raises ValueError.
    """
    text = result.strip()
    literals = won_literals(text, expression=True)
    if len(literals) == 1 and literals[0]["start"] == 0 and literals[0]["end"] == len(text):
        if literals[0]["value"] is None:
            raise ValueError("invalid_won_literal")
        return literals[0]["value"], "원"
    match = re.fullmatch(r"(-?" + _NUMBER + r")(?:[ \t]*(" + _UNIT + r"))?", text)
    if not match:
        raise ValueError("invalid_declared_result")
    return str(Decimal(match[1].replace(",", "")).normalize()), match[2]


def displayed_equation_span(text, amount_start, expression):
    """Find a whole previously verified arithmetic LHS immediately before '= amount'."""
    if not re.fullmatch(r"[0-9.,×*/÷+() \t-]+", expression):
        return None
    prefix = text[:amount_start].rstrip()
    if not prefix.endswith("="):
        return None
    lhs = prefix[:-1].rstrip()
    if not lhs.endswith(expression):
        return None
    start = len(lhs) - len(expression)
    before = lhs[:start].rstrip()
    if before and before[-1] in "0123456789.,×*/÷+-(":
        return None
    return start, len(lhs)


def currency_dimension(expression, amounts, scalar_operands=()):
    """Conservative money dimension check after the existing evaluator succeeds.

    Add/subtract require like units; division by a scalar and multiplication by
    a scalar preserve won. Currency ratios and products cannot justify won.
    Rounding scales must remain independently sourced numeric operands.
    """
    def dimension(node):
        if isinstance(node, ast.Constant):
            value = str(Decimal(str(node.value)).normalize())
            if value in amounts and value in scalar_operands:
                raise ValueError("ambiguous_currency_operand")
            return int(value in amounts)
        if isinstance(node, ast.UnaryOp):
            return dimension(node.operand)
        if isinstance(node, ast.Call):
            return dimension(node.args[0])
        left, right = dimension(node.left), dimension(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mod)):
            if left != right:
                raise ValueError("incompatible_currency_units")
            return left
        if isinstance(node.op, ast.Mult):
            result = left + right
        elif isinstance(node.op, (ast.Div, ast.FloorDiv)):
            result = left - right
        else:
            raise ValueError("incompatible_currency_units")
        if result not in (0, 1):
            raise ValueError("incompatible_currency_units")
        return result
    try:
        return dimension(ast.parse(normalize_expression(expression), mode="eval").body)
    except (ValueError, TypeError, AttributeError, SyntaxError):
        return None


def _same_native_table_with_owned_units(rows, contexts):
    """Cross-row recovery requires one exact parsed table and owned unit refs."""
    identities = set()
    metadata_keys = ("ordin_seq", "source_url", "effective_date", "as_of_date")
    for row in rows:
        # _citations flattens validated source metadata into citation records.
        metadata = tuple(row.get(key) for key in metadata_keys)
        aid = row.get("locator", {}).get("attachment_id")
        refs = row.get("locator", {}).get("source_refs", [])
        if not aid or not all(metadata) or not refs:
            return False
        tables = set()
        for ref in refs:
            capture = (ref.get("attachment_id"), ref.get("parse_file"), ref.get("parse_content_hash"),
                       ref.get("page"), ref.get("element_id"))
            if (capture[0] != aid or not all(capture[:3])
                    or type(capture[3]) is not int or type(capture[4]) is not int
                    or ref.get("representation") != "parse_html" or type(ref.get("row_index")) is not int):
                return False
            tables.add(capture)
        if len(tables) != 1:
            return False
        table = next(iter(tables))
        identities.add((metadata, table))
        owns_unit = False
        for context in contexts:
            if (context.get("kind") != "required_context"
                    or context.get("parent_chunk_id") != row.get("parent_chunk_id")
                    or tuple(context.get(key) for key in metadata_keys) != metadata
                    or not re.search(r"단위\s*[:：]\s*원(?=[\s)）\-/,]|$)", context.get("quote", ""))):
                continue
            unit_refs = context.get("locator", {}).get("source_refs", [])
            if (unit_refs and all((ref.get("attachment_id"), ref.get("parse_file"), ref.get("parse_content_hash")) == table[:3]
                                  for ref in unit_refs)
                    and any(ref.get("page") == table[3] for ref in unit_refs)):
                owns_unit = True
                break
        if not owns_unit:
            return False
    return len(identities) == 1


def native_won_sum_difference(expression, rows, won_table_owners, contexts=()):
    """Recover won only for two owned price atoms, never nested counters.

    A final field must itself name money or a ticket. A service-period field
    may inherit money from its immediately preceding ticket field. Other
    descendants (people, counts, years) do not inherit a price parent's unit.
    """
    if (not rows or not all(row.get("kind") == "attachment"
                       and row.get("parent_chunk_id") in won_table_owners for row in rows)):
        return False
    if (len({row.get("parent_chunk_id") for row in rows}) != 1
            and not _same_native_table_with_owned_units(rows, contexts)):
        return False
    try:
        tree = ast.parse(normalize_expression(expression), mode="eval").body
    except (ValueError, SyntaxError):
        return False
    if (not isinstance(tree, ast.BinOp) or not isinstance(tree.op, (ast.Add, ast.Sub))
            or not all(isinstance(atom, ast.Constant) and type(atom.value) is int
                       for atom in (tree.left, tree.right))):
        return False
    prices = set()
    ticket = r"[가-힣A-Za-z0-9]+권"
    period = r"(?:전일|종일|주간|야간|오전|오후|평일|주말|공휴일|[0-9]+(?:시간|일|개월|월|년))"
    for row in rows:
        for line in row["quote"].splitlines():
            field = re.fullmatch(r"(.+?)\s*[:：]\s*([1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)\s*", line)
            if not field:
                continue
            path = re.sub(r"\s+", "", field[1]).split(">")
            monetary = re.fullmatch(_MONEY_LABEL, path[-1]) or re.fullmatch(ticket, path[-1])
            inherited = (len(path) >= 2 and re.fullmatch(ticket, path[-2])
                         and re.fullmatch(period, path[-1]))
            if monetary or inherited:
                prices.add(str(Decimal(field[2].replace(",", "")).normalize()))
    return all(str(Decimal(atom.value).normalize()) in prices for atom in (tree.left, tree.right))
