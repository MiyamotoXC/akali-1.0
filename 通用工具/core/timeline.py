"""用户端与开发者端共用的时间轴纯函数。"""


def extract_skill_ignites(attacker) -> list:
    """从攻击方技能段提取 calculate_burn 所需的点燃配置。"""
    result = []
    for skill in (getattr(attacker, 'skill_attack', {}) or {},
                  getattr(attacker, 'burst_attack', {}) or {},
                  getattr(attacker, 'support_attack', {}) or {}):
        skill_cd = float(skill.get('cd', 0) or 0)
        for part in skill.get('parts', []) or []:
            ignite = part.get('ignite') if isinstance(part, dict) else None
            if ignite:
                result.append({
                    'count': int(ignite.get('count', 1) or 1),
                    'prob': float(ignite.get('prob', 1.0) or 1.0),
                    'damage': float(ignite.get('dps', 0) or 0),
                    'duration': float(ignite.get('duration', 0) or 0),
                    'cd': float(ignite.get('cd', 0) or 0) or skill_cd,
                })
    return result


def apply_single_skill_buffs(attacker, enemy, choice, normalize_key) -> None:
    """将一次技能释放自身携带的静态增伤和持续效果应用到攻防方副本。"""
    trigger = {2: 'skill', 3: 'burst', 4: 'support'}.get(choice)
    if not trigger:
        return
    attack = {
        'skill': attacker.skill_attack,
        'burst': attacker.burst_attack,
        'support': attacker.support_attack,
    }.get(trigger)
    static_buffs = attack.get('static_buffs') if isinstance(attack, dict) else None
    for key, value in (static_buffs or {}).items():
        normalized = normalize_key(key)
        attacker.buffs[normalized] = attacker.buffs.get(normalized, 0.0) + float(value)
    for item in attacker.skill_buffs or []:
        if item.get('trigger') != trigger:
            continue
        target = item.get('target', 'self') or 'self'
        if target == 'air':
            continue
        target_data = enemy if target == 'enemy' else attacker.buffs
        for key, value in (item.get('buffs') or {}).items():
            normalized = normalize_key(key)
            target_data[normalized] = target_data.get(normalized, 0.0) + float(value)


def build_buff_windows(attacker, user_windows, normalize_key) -> list:
    """把技能持续效果和用户临时效果转换为统一时间轴窗口。"""
    windows = []
    cooldowns = {
        'skill': float(attacker.skill_attack.get('cd', 0) or 0),
        'burst': float(attacker.burst_attack.get('cd', 0) or 0),
        'support': float(attacker.support_attack.get('cd', 0) or 0),
    }
    for item in attacker.skill_buffs or []:
        trigger = item.get('trigger', '')
        cooldown = cooldowns.get(trigger, 0)
        buffs = {normalize_key(k): float(v) for k, v in (item.get('buffs') or {}).items() if v}
        if cooldown <= 0 or not buffs:
            continue
        target = item.get('target', 'self') or 'self'
        windows.append({
            'trigger': trigger, 'cd': cooldown,
            'duration': float(item.get('duration', 0) or 0),
            'buffs': buffs, 'remaining': 0, 'count': 0,
            'target': target, 'before_damage': target != 'enemy',
        })
    for item in user_windows or []:
        if item.get('source') != 'user':
            continue
        buffs = {normalize_key(k): float(v) for k, v in (item.get('buffs') or {}).items() if v}
        if not buffs:
            continue
        duration = float(item.get('duration', 0) or 0)
        windows.append({
            'trigger': 'user', 'cd': 0, 'duration': duration,
            'buffs': buffs, 'remaining': duration, 'count': 1,
            'target': 'self', 'before_damage': True,
        })
    return windows


def active_enemy(base_enemy, windows) -> dict:
    active = dict(base_enemy)
    for window in windows:
        if window['remaining'] > 0 and window.get('target') == 'enemy':
            for key, value in window['buffs'].items():
                active[key] = active.get(key, 0.0) + value
    return active


def active_buffs(base_buffs, windows) -> dict:
    active = dict(base_buffs)
    for window in windows:
        if window['remaining'] > 0:
            for key, value in window['buffs'].items():
                active[key] = active.get(key, 0.0) + value
    return active


def expected_damage(factors, calculate_final_damage) -> float:
    """技能或普攻 factors 转换为期望伤害。"""
    if not factors:
        return 0.0
    if isinstance(factors, list):
        return sum(calculate_final_damage(item)['expected'] for item in factors if item)
    if factors.get('type') == '混合技能':
        return sum(calculate_final_damage(item)['expected'] for item in factors.get('parts', []) if item)
    return calculate_final_damage(factors)['expected']
