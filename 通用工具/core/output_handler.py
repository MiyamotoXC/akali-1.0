# -*- coding: utf-8 -*-
"""
输出处理模块
"""
from typing import Dict, List


class OutputHandler:
    @staticmethod
    def format_single_attack(name: str, factors: Dict) -> str:
        """控制台输出：显示三个核心伤害值（实际/期望/最高暴击）+ 命中率/暴击率"""
        hit_count = factors.get("hit_count", 1)
        base_damage = factors.get("base_damage", 0.0)

        # 1. 未削减伤害
        original = base_damage * hit_count

        # 2. 实际伤害（正常伤害，不暴击，计命中）
        defense_decay = factors.get("defense_decay", 1.0)
        enemy_reduction = factors.get("enemy_reduction", 1.0)
        modifier = factors.get("modifier", 1.0)
        normal = original * defense_decay * enemy_reduction * modifier

        # 3. 最高暴击伤害
        crit_dmg_mult = factors.get("crit_dmg_mult", 0.0)
        critical = normal * (1 + crit_dmg_mult)

        # 4. 期望伤害
        crit_expect = factors.get("crit_expect", 1.0)
        hit_rate = factors.get("hit_rate", 1.0)
        expected = normal * crit_expect * hit_rate

        lines = []
        lines.append(f"  【{name}】")
        lines.append(f"    命中率: {hit_rate:.2%}")
        lines.append(f"    暴击率: {factors.get('crit_rate', 0.0):.2%}")
        lines.append(f"    实际伤害: {normal:.2f}")
        lines.append(f"    期望伤害: {expected:.2f}")
        lines.append(f"    最高暴击伤害: {critical:.2f}")
        return "\n".join(lines)

    @staticmethod
    def format_mixed_skill(name: str, skill_result: Dict) -> str:
        """控制台输出：混合技能汇总四个伤害值"""
        total_original = 0.0
        total_normal = 0.0
        total_critical = 0.0
        total_expected = 0.0

        for part in skill_result.get("parts", []):
            hit_count = part.get("hit_count", 1)
            base_damage = part.get("base_damage", 0.0)
            original = base_damage * hit_count
            defense_decay = part.get("defense_decay", 1.0)
            enemy_reduction = part.get("enemy_reduction", 1.0)
            modifier = part.get("modifier", 1.0)
            normal = original * defense_decay * enemy_reduction * modifier
            crit_dmg_mult = part.get("crit_dmg_mult", 0.0)
            critical = normal * (1 + crit_dmg_mult)
            crit_expect = part.get("crit_expect", 1.0)
            hit_rate = part.get("hit_rate", 1.0)
            expected = normal * crit_expect * hit_rate

            total_original += original
            total_normal += normal
            total_critical += critical
            total_expected += expected

        lines = []
        lines.append(f"  【{name}】")
        lines.append(f"    命中率: {skill_result.get('hit_rate', 1.0):.2%}")
        lines.append(f"    暴击率: {skill_result.get('crit_rate', 0.0):.2%}")
        lines.append(f"    实际伤害: {total_normal:.2f}")
        lines.append(f"    期望伤害: {total_expected:.2f}")
        lines.append(f"    最高暴击伤害: {total_critical:.2f}")
        return "\n".join(lines)

    @staticmethod
    def format_detail_expression(name: str, factors: Dict) -> str:
        """详细计算过程：把公式与各因子数值完整写成式子（供前端折叠块展示）。
        优先使用公式层提供的 proc 全展开过程（每个因子含 buff 运算明细）；
        无 proc 时回退到从 factors 推导的基本式子。"""
        # 优先：公式层全展开过程（攻击力含 buff 运算、防御衰减、增伤区、减伤、克制、暴击等）
        proc = factors.get("proc")
        if proc:
            lines = [f"  【{name}】详细计算过程："]
            for p in proc:
                lines.append("    " + p)
            return "\n".join(lines)

        # 回退：基本式子
        hit_count = factors.get("hit_count", 1)
        base_damage = factors.get("base_damage", 0.0)
        defense_decay = factors.get("defense_decay", 1.0)
        enemy_reduction = factors.get("enemy_reduction", 1.0)
        modifier = factors.get("modifier", 1.0)
        crit_dmg_mult = factors.get("crit_dmg_mult", 0.0)
        crit_expect = factors.get("crit_expect", 1.0)
        hit_rate = factors.get("hit_rate", 1.0)

        original = base_damage * hit_count
        normal = original * defense_decay * enemy_reduction * modifier
        critical = normal * (1 + crit_dmg_mult)
        expected = normal * crit_expect * hit_rate

        lines = [f"  【{name}】详细计算过程："]
        lines.append(f"    基础伤害 = 最终攻击力 {factors.get('final_attack', 0):.2f} × 倍率 {factors.get('multiplier', 1.0):.2f} × 命中段数 {hit_count} = {original:.2f}")
        lines.append(f"    实际伤害 = 基础伤害 {original:.2f} × 防御衰减 {defense_decay:.4f} × 敌方减伤 {enemy_reduction:.4f} × 克制修正 {modifier:.4f} = {normal:.2f}")
        lines.append(f"    最高暴击伤害 = 实际伤害 {normal:.2f} × (1 + 暴击伤害倍率 {crit_dmg_mult:.4f}) = {critical:.2f}")
        lines.append(f"    期望伤害 = 实际伤害 {normal:.2f} × 暴击期望 {crit_expect:.4f} × 命中率 {hit_rate:.4f} = {expected:.2f}")
        lines.append(f"    （命中率 {hit_rate:.2%}，暴击率 {factors.get('crit_rate', 0):.2%}，增伤系数 {factors.get('damage_increase', 0):.4f}，敌方减伤 {enemy_reduction:.4f}）")
        return "\n".join(lines)

    @staticmethod
    def format_results(results: Dict, choices: List[str]) -> str:
        output_lines = []
        output_lines.append("\n" + "=" * 60)
        output_lines.append("             伤害计算结果")
        output_lines.append("=" * 60)
        # 普攻输出备注（放在开头，避免被"详细计算过程"折叠截断）
        if "普攻" in choices:
            output_lines.append("备注：普攻实战会丢很多伤害。")

        for key in choices:
            result = results.get(key)
            if result is None:
                output_lines.append(f"  {key}: 未配置或未计算")
                continue

            if key == "普攻":
                for idx, factors in enumerate(result, 1):
                    output_lines.append(OutputHandler.format_single_attack(f"普攻 {idx}", factors))
                    output_lines.append(OutputHandler.format_detail_expression(f"普攻 {idx}", factors))
            elif key in ["通常技", "必杀技", "支援技", "点燃"]:
                if result.get("type") == "混合技能":
                    output_lines.append(OutputHandler.format_mixed_skill(key, result))
                    # 混合技能：逐子项输出详细式子
                    for part in result.get("parts", []):
                        output_lines.append(OutputHandler.format_detail_expression(f"{key}-{part.get('damage_type', '')}", part))
                else:
                    output_lines.append(OutputHandler.format_single_attack(key, result))
                    output_lines.append(OutputHandler.format_detail_expression(key, result))
            else:
                output_lines.append(f"  {key}: 未知格式")

        output_lines.append("=" * 60)
        return "\n".join(output_lines)

    # ==================== 调试日志写入（含中文因子说明） ====================

    @staticmethod

    @staticmethod
    def _write_factors_cn(f, factors: Dict, factor_cn: Dict, indent: str = ""):
        """写入带中文说明的因子"""
        order = [
            "damage_type", "weapon_type", "ammo_type",
            "base_attack", "final_attack",
            "defense_decay", "crit_expect", "crit_rate", "crit_dmg_mult",
            "hit_rate", "damage_increase", "enemy_reduction", "modifier",
            "multiplier", "hit_count", "base_damage", "cd"
        ]
        for key in order:
            if key in factors:
                val = factors[key]
                cn_name = factor_cn.get(key, key)
                if isinstance(val, float):
                    f.write(f"{indent}{cn_name}: {val:.4f}\n")
                else:
                    f.write(f"{indent}{cn_name}: {val}\n")
        # 输出其他未在 order 中的键
        for key, val in factors.items():
            if key not in order and key not in ["index", "type"]:
                cn_name = factor_cn.get(key, key)
                if isinstance(val, float):
                    f.write(f"{indent}{cn_name}: {val:.4f}\n")
                else:
                    f.write(f"{indent}{cn_name}: {val}\n")

    # ==================== 新增：总伤害计算辅助方法 ====================
    @staticmethod
    def calculate_final_damage(factors: Dict) -> Dict:
        """根据因子字典计算最终伤害（正常、暴击、期望）"""
        base = factors["base_damage"]
        defense_decay = factors["defense_decay"]
        enemy_reduction = factors["enemy_reduction"]
        modifier = factors["modifier"]
        hit_count = factors["hit_count"]
        crit_dmg_mult = factors["crit_dmg_mult"]
        crit_expect = factors["crit_expect"]

        normal_per_hit = base * defense_decay * enemy_reduction * modifier
        total_normal = normal_per_hit * hit_count
        total_critical = total_normal * (1 + crit_dmg_mult)
        total_expected = total_normal * crit_expect

        return {
            "normal": total_normal,
            "critical": total_critical,
            "expected": total_expected,
            "hit_rate": factors.get("hit_rate", 1.0),
            "crit_rate": factors.get("crit_rate", 0.0),
            "crit_dmg_mult": crit_dmg_mult,
        }

    @staticmethod
    def format_burn_result(burn_result: Dict) -> str:
        """格式化单次点燃计算结果（choice=5）"""
        if burn_result is None:
            return "点燃结果: 无数据"
        if "error" in burn_result:
            return f"⚠️ {burn_result['error']}"

        lines = []
        lines.append("\n" + "=" * 60)
        lines.append("             点燃伤害计算结果")
        lines.append("=" * 60)
        lines.append(f"燃烧总时间: {burn_result.get('burn_time', 0):.1f}秒")
        # 只输出计命中+概率的实际伤害（点燃伤害已考虑命中率与点燃概率）
        lines.append(f"点燃实际伤害(计命中+概率): {burn_result.get('total_actual_ignite', 0):.2f}")
        lines.append("备注：普攻实战会丢很多伤害。")

        details = burn_result.get("details", [])
        if details:
            lines.append("\n各普攻点燃明细:")
            for d in details:
                lines.append(f"  {d['source']}: 触发{d['触发次数']}次, 实际总伤害={d['实际总伤害']:.2f}")
        lines.append("=" * 60)
        return "\n".join(lines)