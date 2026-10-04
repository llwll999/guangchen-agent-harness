"""Reject ambiguous object keys and non-finite numbers at JSON boundaries."""

import json
import math


def strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 对象含重复字段。")
            result[key] = value
        return result

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("JSON 数字必须有限。")
        return value

    def reject_constant(text):
        raise ValueError("JSON 不允许 NaN 或 Infinity。")

    return json.loads(raw, object_pairs_hook=pairs, parse_float=finite_float,
                      parse_constant=reject_constant)
