REASONS = {"COLUMNS":"列数不符", "HEADER":"字段缺失", "MISSING":"必填值为空", "NUMBER":"数值无效", "RANGE":"超出范围", "DUPLICATE":"相同行重复", "CONFLICT":"同键数值冲突", "VERSION":"版本冲突", "EMPTY":"输入为空", "STRUCTURE":"文件结构错误", "CORRECTION_SOURCE":"原回执不存在", "CORRECTION_DUPLICATE":"纠错已存在且未回退", "CORRECTION_STATE":"纠错状态不允许该操作", "CORRECTION_EMPTY":"纠错没有有效证书", "CORRECTION_QUARANTINE":"纠错行全部被隔离"}
def isolate(ctx, row, code, detail):
    ctx['quarantine'].append({**row, '原因码': code, '原因': detail})
