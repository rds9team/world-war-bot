import discord
from discord import app_commands
from discord.ext import commands
import random
import datetime
import asyncio
import os
import sqlite3

from main import (
    get_db_connection, safe_defer, send_dm_fallback, ensure_world_context,
    get_promo_and_tip, get_paypay_link, resolve_country_code, check_and_create_user,
    COUNTRY_MAP, is_oil_enabled, BASE_INCOME, TERRITORY_YIELD, _generate_current_map_sync,
    add_world_log, add_trophy
)

from cogs.ui_logic import (
    work_cooldowns, OilImportView, CommandGUIView, HelpView, AttackTargetModal,
    DefendView, execute_defend_logic, run_status, run_diplomacy, run_targets,
    run_country_management, run_country_status, run_attack, run_un_list, run_camp_list,
    my_countries_autocomplete, run_version, run_update, StatsView, PeaceTreatyView,
    run_start_wizard, build_dashboard_embed
)

class CommandsCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="user_setting", description="ユーザー固有の便利設定（確認ダイアログのON/OFFなど）を行います")
    @app_commands.choices(setting=[app_commands.Choice(name="攻撃時の確認ダイアログ", value="confirm_attack")])
    @app_commands.choices(mode=[app_commands.Choice(name="ONにする", value=1), app_commands.Choice(name="OFFにする", value=0)])
    async def cmd_user_setting(self, interaction: discord.Interaction, setting: app_commands.Choice[str], mode: app_commands.Choice[int]):
        await safe_defer(interaction, ephemeral=True)
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO user_settings (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
            if setting.value == "confirm_attack": c.execute("UPDATE user_settings SET confirm_attack=? WHERE guild_id=? AND user_id=?", (mode.value, guild_id, user_id))
            conn.commit()
        msg = f"[設定] **{setting.name}** を **{'ON' if mode.value==1 else 'OFF'}** に変更しました。"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="help", description="ゲームのルール、初期設定、遊び方の詳細を説明します")
    async def cmd_help(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        view = HelpView()
        await interaction.followup.send(content=get_promo_and_tip(), embed=view.generate_embed("basic"), view=view)

    @app_commands.command(name="donate", description="開発者への支援リンクを表示します")
    async def cmd_donate(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        msg = f"**開発者支援(PayPay)**\n{get_paypay_link()}"
        await interaction.followup.send(msg + get_promo_and_tip())

    @app_commands.command(name="work", description="労働して資金を稼ぎます（国連加盟国にはボーナスあり）")
    async def cmd_work(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        now = datetime.datetime.now(datetime.timezone.utc)
        key = (guild_id, world_id, user_id)
        if key in work_cooldowns:
            elapsed = (now - work_cooldowns[key]).total_seconds()
            if elapsed < 3600:
                minutes, seconds = divmod(int(3600 - elapsed), 60)
                return await interaction.followup.send(f"まだ準備中です。あと {minutes}分 {seconds}秒 お待ちください。", ephemeral=True)
                
        work_cooldowns[key] = now
        earned_gold = random.randint(300, 800)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            check_and_create_user(c, guild_id, world_id, user_id, interaction.user.display_name)
            c.execute("SELECT 1 FROM un_members WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            is_un = c.fetchone()
            
            if is_un:
                earned_gold = int(earned_gold * 1.5)
                earned_oil = random.randint(75, 225)
                c.execute("UPDATE players SET gold=gold+?, oil=oil+? WHERE guild_id=? AND world_id=? AND user_id=?", (earned_gold, earned_oil, guild_id, world_id, user_id))
                msg_bonus = f"\n[国連ボーナス] 追加資金と石油(**{earned_oil} L**)を採掘しました！"
            else:
                earned_oil = random.randint(50, 150)
                c.execute("UPDATE players SET gold=gold+?, oil=oil+? WHERE guild_id=? AND world_id=? AND user_id=?", (earned_gold, earned_oil, guild_id, world_id, user_id))
                msg_bonus = f"\n[通常採掘] 石油(**{earned_oil} L**)を採掘しました！"
                
            c.execute("SELECT gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            current_gold, current_oil = c.fetchone()
            conn.commit()
            
        msg = f"[完了] 労働をして [世界#{world_id}] で `{earned_gold}` Gold稼ぎました！{msg_bonus}\n所持金: **{current_gold} Gold** / 石油: **{current_oil} L**"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="war_bonds", description="資金が尽きた時、戦債を発行して資金を借ります(給付から天引き)")
    async def cmd_war_bonds(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            check_and_create_user(c, guild_id, world_id, user_id, interaction.user.display_name)
            c.execute("SELECT gold, debt FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            gold, debt = row[0], row[1] if row else (0, 0)
            if gold >= 1000: return await interaction.followup.send("[エラー] 戦債は資金が **1000 Gold未満** の場合のみ発行可能です。", ephemeral=True)
            if debt > 0: return await interaction.followup.send("[エラー] すでに戦債を発行しています。完済するまで再発行はできません。", ephemeral=True)
            c.execute("UPDATE players SET gold=gold+5000, debt=5000 WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            conn.commit()
        msg = f"[世界#{world_id}] **戦債を発行しました！**\n国庫から **5000 Gold** を借り入れました。今後の定時給付の際に、収入の一部が自動的に返済へ充てられます。"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="import_oil", description="指定したプレイヤーにオイル(Oil)の輸入(購入)を申請します")
    async def cmd_import_oil(self, interaction: discord.Interaction, target: discord.Member, amount: int):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if amount <= 0: return await interaction.followup.send("[エラー] 1L以上を指定してください。", ephemeral=True)
        if target.bot or interaction.user.id == target.id: return await interaction.followup.send("[エラー] 自分やBotには申請できません。", ephemeral=True)
        
        guild_id, user_id, target_id = str(interaction.guild_id), str(interaction.user.id), str(target.id)
        price = int(amount * 100 / 150)
        with get_db_connection() as conn:
            c = conn.cursor()
            check_and_create_user(c, guild_id, world_id, user_id, interaction.user.display_name)
            c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            if not row or row[0] < price: return await interaction.followup.send(f"[エラー] 資金不足です。({amount} L の輸入には **{price} Gold** 必要です)", ephemeral=True)

        view = OilImportView(guild_id, world_id, user_id, target_id, amount, price)
        content = f"**[オイル輸入申請 / 世界#{world_id}]**\n<@{user_id}> があなたから **{amount} L** のオイルを **{price} Gold** で購入したがっています。\n許可しますか？"
        if await send_dm_fallback(target, interaction.channel, content, view): await interaction.followup.send("[完了] 相手にオイルの輸入申請(DM)を送信しました。", ephemeral=True)
        else: await interaction.followup.send("[完了] 相手にオイルの輸入申請を送信しました。", ephemeral=True)

    @app_commands.command(name="convert_resource", description="システム(国庫)とゴールド・オイルを交換します")
    @app_commands.choices(exchange_type=[
        app_commands.Choice(name="Goldを払ってOilを得る (100G → 150L)", value="gold_to_oil"),
        app_commands.Choice(name="Oilを払ってGoldを得る (150L → 100G)", value="oil_to_gold")
    ])
    async def cmd_convert_resource(self, interaction: discord.Interaction, exchange_type: app_commands.Choice[str], amount: int):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if amount <= 0: return await interaction.followup.send("[エラー] 1以上を指定してください。", ephemeral=True)
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            if not row: return await interaction.followup.send("[エラー] データが見つかりません。", ephemeral=True)
            gold, oil = row[0], row[1]
            if exchange_type.value == "gold_to_oil":
                if gold < amount: return await interaction.followup.send(f"[エラー] 資金不足です。({amount} Gold 必要)", ephemeral=True)
                oil_gain = int(amount * 150 / 100)
                c.execute("UPDATE players SET gold=gold-?, oil=oil+? WHERE guild_id=? AND world_id=? AND user_id=?", (amount, oil_gain, guild_id, world_id, user_id))
                msg = f"[完了] 国庫に **{amount} Gold** を支払い、**{oil_gain} L** のオイルを獲得しました！"
            else:
                if oil < amount: return await interaction.followup.send(f"[エラー] オイル不足です。({amount} L 必要)", ephemeral=True)
                gold_gain = int(amount * 100 / 150)
                c.execute("UPDATE players SET oil=oil-?, gold=gold+? WHERE guild_id=? AND world_id=? AND user_id=?", (amount, gold_gain, guild_id, world_id, user_id))
                msg = f"[完了] 国庫に **{amount} L** のオイルを売却し、**{gold_gain} Gold** を獲得しました！"
            conn.commit()
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="pay", description="指定したプレイヤーに自分の資金(Gold)を送金します")
    async def cmd_pay(self, interaction: discord.Interaction, target: discord.Member, amount: int):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if amount <= 0: return await interaction.followup.send("[エラー] 送金額は1以上にしてください。")
        if target.bot or interaction.user.id == target.id: return await interaction.followup.send("[エラー] 自分やBotには送金できません。")
        guild_id, user_id, target_id = str(interaction.guild_id), str(interaction.user.id), str(target.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            if not row or row[0] < amount: return await interaction.followup.send("[エラー] 資金不足です。")
            check_and_create_user(c, guild_id, world_id, target_id, target.display_name)
            c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (amount, guild_id, world_id, user_id))
            c.execute("UPDATE players SET gold=gold+? WHERE guild_id=? AND world_id=? AND user_id=?", (amount, guild_id, world_id, target_id))
            conn.commit()
        await interaction.followup.send(f"[送金完了 / 世界#{world_id}] <@{user_id}> が <@{target_id}> に **{amount} Gold** を送金しました！" + get_promo_and_tip())

    @app_commands.command(name="say", description="指定したユーザーまたは陣営にプライベートメッセージを送信します")
    async def cmd_say(self, interaction: discord.Interaction, target: str, message: str):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM camp_members WHERE guild_id=? AND world_id=? AND camp_name=?", (str(interaction.guild_id), world_id, target))
            camp_members = c.fetchall()

        if camp_members:
            success_count = 0
            for (uid,) in camp_members:
                member = interaction.guild.get_member(int(uid))
                if member and not member.bot:
                    try: await member.send(f"**[陣営通信: {target}]** {interaction.user.display_name} より:\n{message}"); success_count += 1
                    except: pass
            await interaction.followup.send(f"[完了] 陣営「{target}」のメンバー {success_count} 名に送信しました。", ephemeral=True)
        else:
            target_member = discord.utils.find(lambda m: target.lower() in m.display_name.lower() or target.lower() in m.name.lower(), interaction.guild.members)
            if not target_member: return await interaction.followup.send("[エラー] 見つかりませんでした。", ephemeral=True)
            try:
                await target_member.send(f"**[通信]** {interaction.user.display_name} より:\n{message}")
                await interaction.followup.send(f"[完了] {target_member.display_name} に送信しました。", ephemeral=True)
            except: await interaction.followup.send("[エラー] 送信に失敗しました。", ephemeral=True)

    @app_commands.command(name="country_name", description="自分が所有している領土からメイン国家を設定します")
    @app_commands.autocomplete(target_code=my_countries_autocomplete)
    async def cmd_country_name(self, interaction: discord.Interaction, target_code: str):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM territories WHERE guild_id=? AND world_id=? AND owner_id=? AND iso_alpha=?", (guild_id, world_id, user_id, target_code))
            if not c.fetchone(): return await interaction.followup.send("[エラー] 所有していない国です。", ephemeral=True)
            c.execute("UPDATE players SET main_country=? WHERE guild_id=? AND world_id=? AND user_id=?", (target_code, guild_id, world_id, user_id))
            conn.commit()
        await interaction.followup.send(f"[設定] [世界#{world_id}] メイン国家を **【{target_code}】** に設定しました！")

    @app_commands.command(name="targets", description="現在攻撃可能な国の一覧を確認します")
    async def cmd_targets(self, interaction: discord.Interaction):
        await run_targets(interaction)

    @app_commands.command(name="gui", description="宣戦布告や同盟提案などの外交アクションをGUIで行います")
    async def cmd_diplomacy(self, interaction: discord.Interaction):
        await run_diplomacy(interaction)

    @app_commands.command(name="attack", description="指定した国に対する作戦を立案します(オプションで別の世界を指定可)")
    @app_commands.choices(world_num=[app_commands.Choice(name="世界 #1", value=1), app_commands.Choice(name="世界 #2", value=2), app_commands.Choice(name="世界 #3", value=3)])
    async def cmd_attack(self, interaction: discord.Interaction, target: str, world_num: app_commands.Choice[int] = None):
        w_id = world_num.value if world_num else None
        await run_attack(interaction, target, world_id_override=w_id)

    @app_commands.command(name="status", description="自分の現在の資金、総兵力、借金(戦債)などを確認します")
    async def cmd_status(self, interaction: discord.Interaction):
        await run_status(interaction)

    @app_commands.command(name="map", description="現在の世界の戦況マップを表示します(オプションで別の世界を指定可)")
    @app_commands.choices(world_num=[app_commands.Choice(name="世界 #1", value=1), app_commands.Choice(name="世界 #2", value=2), app_commands.Choice(name="世界 #3", value=3)])
    async def cmd_map(self, interaction: discord.Interaction, world_num: app_commands.Choice[int] = None):
        await safe_defer(interaction)
        if world_num is not None:
            w_id = world_num.value
        else:
            w_id = await ensure_world_context(interaction)
            if w_id == 0: return

        map_file, occupied_lands = await asyncio.to_thread(_generate_current_map_sync, str(interaction.guild_id), w_id)
        embed = discord.Embed(title=f"世界情勢 [世界#{w_id}]", color=0x3498db)
        owner_dict = {}
        for iso, _, _, _, u_name in occupied_lands: owner_dict.setdefault(u_name, []).append(iso)
        desc = "\n".join([f"**{u_name}**: {', '.join(lands)}" for u_name, lands in owner_dict.items()])
        embed.description = "【支配状況】\n" + desc[:4000] if desc else "現在、支配されている領土はありません。"
        if map_file: await interaction.followup.send(content=get_promo_and_tip(), embed=embed.set_image(url="attachment://war_map.png"), file=map_file)
        else: await interaction.followup.send(content=get_promo_and_tip(), embed=embed)

    @app_commands.command(name="country_management", description="所有している領土の管理(所有権放棄など)をGUIで行います")
    async def cmd_country_management(self, interaction: discord.Interaction):
        await run_country_management(interaction)

    @app_commands.command(name="country_status", description="自分の領土の防衛力一覧を確認します")
    async def cmd_country_status(self, interaction: discord.Interaction):
        await run_country_status(interaction)

    @app_commands.command(name="defend", description="領土に防衛費を投資します(未指定で複数選択メニューを表示。ALLで全領土指定)")
    async def cmd_defend(self, interaction: discord.Interaction, target: str = None, amount: int = None):
        if target is None or amount is None:
            world_id = await ensure_world_context(interaction)
            if world_id == 0: return
            guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
                territories = c.fetchall()
            if not territories:
                msg = "[エラー] 所有している領土がありません。"
                if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
                else: await interaction.response.send_message(msg, ephemeral=True)
                return
            if interaction.response.is_done(): await interaction.followup.send("防衛費を投資する領土を選択してください。", view=DefendView(territories, world_id), ephemeral=True)
            else: await interaction.response.send_message("防衛費を投資する領土を選択してください。", view=DefendView(territories, world_id), ephemeral=True)
            return

        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return

        target_codes = []
        if target.upper() == "ALL":
            guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
                target_codes = [r[0] for r in c.fetchall()]
        else: target_codes = [resolve_country_code(t.strip()) for t in target.split(",") if t.strip()]

        if not target_codes: return await interaction.followup.send("[エラー] 有効な国コードが指定されていません。", ephemeral=True)
        await execute_defend_logic(interaction, target_codes, amount, world_id)

    @app_commands.command(name="withdraw", description="自国領土の防衛力を資金(ゴールド)に戻します")
    async def cmd_withdraw(self, interaction: discord.Interaction, target: str, amount: int):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if amount <= 0: return await interaction.followup.send("[エラー] 1以上にしてください。", ephemeral=True)
        guild_id, user_id, code = str(interaction.guild_id), str(interaction.user.id), resolve_country_code(target)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=? AND owner_id=?", (guild_id, world_id, code, user_id))
            row = c.fetchone()
            if not row: return await interaction.followup.send("[エラー] あなたの領土ではありません。", ephemeral=True)
            if row[0] - amount < 100: return await interaction.followup.send(f"[エラー] 防衛力は最低100必要です。", ephemeral=True)
            c.execute("UPDATE territories SET defense=defense-? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (amount, guild_id, world_id, code))
            c.execute("UPDATE players SET gold=gold+? WHERE guild_id=? AND world_id=? AND user_id=?", (amount, guild_id, world_id, user_id))
            conn.commit()
        msg = f"[完了] {code} から兵力を {amount}人 引き戻しました。"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="reallocate", description="自国の防衛力を別の自国領土に割り当て直します")
    async def cmd_reallocate(self, interaction: discord.Interaction, from_country: str, to_country: str, amount: int):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if amount <= 0: return await interaction.followup.send("[エラー] 1以上にしてください。", ephemeral=True)
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        f_code, t_code = resolve_country_code(from_country), resolve_country_code(to_country)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=? AND owner_id=?", (guild_id, world_id, f_code, user_id))
            f_row = c.fetchone()
            c.execute("SELECT defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=? AND owner_id=?", (guild_id, world_id, t_code, user_id))
            t_row = c.fetchone()
            if not f_row or not t_row: return await interaction.followup.send("[エラー] 両方ともあなたの領土である必要があります。", ephemeral=True)
            if f_row[0] - amount < 100: return await interaction.followup.send(f"[エラー] 移動後、{f_code} の防衛力は最低100必要です。", ephemeral=True)
            c.execute("UPDATE territories SET defense=defense-? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (amount, guild_id, world_id, f_code))
            c.execute("UPDATE territories SET defense=defense+? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (amount, guild_id, world_id, t_code))
            conn.commit()
        msg = f"[完了] {f_code} から {t_code} へ {amount} 移動しました。"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="code", description="使用可能な国名・国コードの一覧を確認します")
    async def cmd_code(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        unique_codes = {code: name for name, code in COUNTRY_MAP.items() if not name.isascii()}
        embed = discord.Embed(title="国コード＆国名一覧", description="コマンドへはコード・国名どちらを入力しても動きます。", color=0x3498db)
        current_text = ""
        for code, name in sorted(unique_codes.items()):
            item = f"`{code}:{name}` "
            if len(current_text) + len(item) > 1000:
                embed.add_field(name="\u200b", value=current_text, inline=False); current_text = item
            else: current_text += item
        if current_text: embed.add_field(name="\u200b", value=current_text, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="un_invite", description="指定したプレイヤーを国連(UN)に招待します")
    async def cmd_un_invite(self, interaction: discord.Interaction, target: discord.Member):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if target.bot: return await interaction.followup.send("[エラー] Botは招待できません。")
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO un_invites (guild_id, world_id, user_id) VALUES (?, ?, ?)", (str(interaction.guild_id), world_id, str(target.id)))
            conn.commit()
        await interaction.followup.send(f"[完了 / 世界#{world_id}] <@{target.id}> を国連に招待しました。")

    @app_commands.command(name="un_join", description="国連への招待を承諾して加入します")
    async def cmd_un_join(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM un_invites WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            if not c.fetchone(): return await interaction.followup.send("[エラー] 招待されていません。")
            c.execute("DELETE FROM un_invites WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            c.execute("INSERT OR IGNORE INTO un_members (guild_id, world_id, user_id) VALUES (?, ?, ?)", (guild_id, world_id, user_id))
            conn.commit()
        await interaction.followup.send(f"[完了 / 世界#{world_id}] <@{user_id}> が国連に加入しました。")

    @app_commands.command(name="un_leave", description="国連から脱退します")
    async def cmd_un_leave(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM un_members WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            if not c.fetchone(): return await interaction.followup.send("[エラー] 加入していません。")
            c.execute("DELETE FROM un_members WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            conn.commit()
        await interaction.followup.send(f"[完了] <@{user_id}> が国連から脱退しました。")

    @app_commands.command(name="un_list", description="現在の国連(UN)加盟国一覧を確認します")
    async def cmd_un_list_group(self, interaction: discord.Interaction):
        await run_un_list(interaction)

    @app_commands.command(name="camp_create", description="新しい陣営を設立します")
    async def cmd_camp_create(self, interaction: discord.Interaction, camp_name: str):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM camps WHERE guild_id=? AND world_id=? AND camp_name=?", (guild_id, world_id, camp_name))
            if c.fetchone(): return await interaction.followup.send("[エラー] 既に存在します。")
            c.execute("INSERT INTO camps (guild_id, world_id, camp_name, founder_id) VALUES (?, ?, ?, ?)", (guild_id, world_id, camp_name, user_id))
            c.execute("INSERT INTO camp_members (guild_id, world_id, user_id, camp_name) VALUES (?, ?, ?, ?)", (guild_id, world_id, user_id, camp_name))
            conn.commit()
        await interaction.followup.send(f"[完了] 新たな陣営 **【{camp_name}】** が設立されました！")

    @app_commands.command(name="camp_invite", description="指定したプレイヤーを陣営に招待します")
    async def cmd_camp_invite(self, interaction: discord.Interaction, target: discord.Member, camp_name: str):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        if target.bot: return await interaction.followup.send("[エラー] Botは招待できません。")
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM camp_members WHERE guild_id=? AND world_id=? AND user_id=? AND camp_name=?", (str(interaction.guild_id), world_id, str(interaction.user.id), camp_name))
            if not c.fetchone(): return await interaction.followup.send("[エラー] メンバーではありません。")
            c.execute("INSERT OR REPLACE INTO camp_invites (guild_id, world_id, user_id, camp_name) VALUES (?, ?, ?, ?)", (str(interaction.guild_id), world_id, str(target.id), camp_name))
            conn.commit()
        await interaction.followup.send(f"[完了] <@{target.id}> を陣営 **【{camp_name}】** に招待しました。")

    @app_commands.command(name="camp_join", description="招待された陣営に加入します")
    async def cmd_camp_join(self, interaction: discord.Interaction, camp_name: str):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM camp_invites WHERE guild_id=? AND world_id=? AND user_id=? AND camp_name=?", (guild_id, world_id, user_id, camp_name))
            if not c.fetchone(): return await interaction.followup.send("[エラー] 招待されていません。")
            c.execute("DELETE FROM camp_invites WHERE guild_id=? AND world_id=? AND user_id=? AND camp_name=?", (guild_id, world_id, user_id, camp_name))
            c.execute("INSERT OR IGNORE INTO camp_members (guild_id, world_id, user_id, camp_name) VALUES (?, ?, ?, ?)", (guild_id, world_id, user_id, camp_name))
            conn.commit()
        await interaction.followup.send(f"[完了] <@{user_id}> が陣営 **【{camp_name}】** に加入しました。")

    @app_commands.command(name="invite", description="指定したプレイヤーを陣営に招待します (/invite_camp と同じ)")
    async def cmd_invite(self, interaction: discord.Interaction, target: discord.Member, camp_name: str):
        await self.cmd_invite_camp.callback(self, interaction, target, camp_name)

    @app_commands.command(name="camp_list", description="現在の陣営一覧を確認します")
    async def cmd_camp_list_group(self, interaction: discord.Interaction):
        await run_camp_list(interaction)

    @app_commands.command(name="join", description="公式サポートサーバーへの参加リンクを表示します")
    async def cmd_join(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        await interaction.followup.send("公式サポートサーバーへの参加はこちら！\nバグ報告や質問、他プレイヤーとの交流にどうぞ。\nhttps://discord.gg/km2VDjCNpE")

    @app_commands.command(name="world_setting", description="自分が行動する世界を手動設定します（チャンネル未設定サーバー用）")
    @app_commands.choices(world_num=[app_commands.Choice(name="世界 #1", value=1), app_commands.Choice(name="世界 #2", value=2), app_commands.Choice(name="世界 #3", value=3)])
    async def cmd_world_setting(self, interaction: discord.Interaction, world_num: app_commands.Choice[int]):
        await safe_defer(interaction, ephemeral=True)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO user_settings (guild_id, user_id, active_world) VALUES (?, ?, ?)", (str(interaction.guild_id), str(interaction.user.id), world_num.value))
            conn.commit()
        await interaction.followup.send(f"[設定] あなたのアクティブな世界を **世界 #{world_num.value}** に設定しました。", ephemeral=True)

    @app_commands.command(name="start", description="【初心者向け】好きな国（首都）を選んで自分の国家を建国します")
    async def cmd_start(self, interaction: discord.Interaction):
        await run_start_wizard(interaction)

    @app_commands.command(name="command", description="国家司令部ダッシュボードを開き、ボタン操作でゲームをプレイします")
    async def cmd_command(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id, user_name = str(interaction.guild_id), str(interaction.user.id), interaction.user.display_name
        embed, my_lands = build_dashboard_embed(guild_id, world_id, user_id, user_name)
        view = CommandGUIView(world_id, my_lands)
        await interaction.followup.send(content=get_promo_and_tip(), embed=embed, view=view, ephemeral=True)
    @app_commands.command(name="version", description="閲覧可能なアップデートのバージョン一覧を最新から10件表示します")
    async def cmd_version(self, interaction: discord.Interaction):
        await run_version(interaction)

    @app_commands.command(name="update", description="指定したバージョン（未指定で最新）のアップデート情報を表示します")
    @app_commands.describe(version="確認したいバージョン (例: v1.0)")
    async def cmd_update(self, interaction: discord.Interaction, version: str = None):
        await run_update(interaction, version)

    @app_commands.command(name="stats", description="自国の詳細ステータスを表示します(自分のみ閲覧可能)")
    async def cmd_stats(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        db_path = os.getenv("DB_FILE", "war_game_worlds.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT active_world FROM user_settings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            world_row = cursor.fetchone()
            world_id = world_row["active_world"] if world_row else 1

            cursor.execute("SELECT * FROM players WHERE guild_id = ? AND world_id = ? AND user_id = ?", (guild_id, world_id, user_id))
            player_row = cursor.fetchone()

            if player_row is None:
                return await interaction.followup.send("国家データが見つかりません。まずは建国してください。", ephemeral=True)

            cursor.execute(
                "SELECT COUNT(iso_alpha) as t_count, SUM(defense) as total_def FROM territories WHERE guild_id = ? AND world_id = ? AND owner_id = ?", 
                (guild_id, world_id, user_id)
            )
            terr_row = cursor.fetchone()
            territory_count = terr_row["t_count"] if (terr_row and terr_row["t_count"]) else 0
            total_defense = terr_row["total_def"] if (terr_row and terr_row["total_def"]) else 0

            db_keys = player_row.keys()
            user_data = {
                "country_name": player_row["main_country"] if "main_country" in db_keys else "未設定",
                "user_name": player_row["user_name"] if "user_name" in db_keys else interaction.user.display_name,
                "money": player_row["gold"] if "gold" in db_keys else 0,
                "oil": player_row["oil"] if "oil" in db_keys else 0,
                "debt": player_row["debt"] if "debt" in db_keys else 0,
                "territory": territory_count,
                "defense": total_defense,
                "invest": player_row["invest"] if "invest" in db_keys else 0,
                "tech_level": player_row["tech_level"] if "tech_level" in db_keys else 1,
                "title": player_row["title"] if "title" in db_keys else "未設定",
                "wins": player_row["wins"] if "wins" in db_keys else 0,
                "losses": player_row["losses"] if "losses" in db_keys else 0,
                "trophy_count": player_row["trophy_count"] if "trophy_count" in db_keys else 0,
                "status_effects": "なし",
                "rank": "集計中" 
            }
        except sqlite3.Error as e:
            return await interaction.followup.send(f"データベースの読み込みに失敗しました: {e}", ephemeral=True)
        finally:
            conn.close()

        view = StatsView(author_id=interaction.user.id, user_data=user_data)
        await interaction.followup.send(embed=view.generate_embed("economy"), view=view, ephemeral=True)
    @app_commands.command(name="invest", description="国家予算を投資し、長期的な利益や技術力を高めます")
    async def cmd_invest(self, interaction: discord.Interaction, amount: int):
        await safe_defer(interaction, ephemeral=True)
        
        if amount <= 0: 
            return await interaction.followup.send("[エラー] 投資額は1 Gold以上にしてください。", ephemeral=True)
        
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            # プレイヤーの資金と現在の投資額を取得
            c.execute("SELECT gold, invest FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            
            if not row:
                return await interaction.followup.send("[エラー] 国家データが見つかりません。まずは建国してください。", ephemeral=True)
            
            current_gold, current_invest = row[0], (row[1] if row[1] else 0)
            
            if current_gold < amount:
                return await interaction.followup.send(f"[エラー] 資金不足です。(現在: **{current_gold} Gold**)", ephemeral=True)
            
            # 投資処理（資金を減らし、投資額を増やす）
            new_gold = current_gold - amount
            new_invest = current_invest + amount
            
            c.execute("UPDATE players SET gold=?, invest=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold, new_invest, guild_id, world_id, user_id))
            conn.commit()
            
        msg = f"📈 **[投資完了]**\n国家インフラに **{amount} Gold** を投資しました！\n現在の総投資額: **{new_invest}**"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)


    @app_commands.command(name="research", description="技術研究を行い、国家のテックレベルを上昇させます")
    async def cmd_research(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT gold, oil, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            
            if not row:
                return await interaction.followup.send("[エラー] 国家データが見つかりません。", ephemeral=True)
            
            gold, oil = row[0], row[1]
            tech_level = row[2] if row[2] else 1
            
            # レベルに応じたコスト計算 (例: Lv1->2は 1000Gold & 500Oil。Lvが上がるごとに増大)
            cost_gold = tech_level * 1000
            cost_oil = tech_level * 500
            
            if gold < cost_gold or oil < cost_oil:
                msg = f"🧪 **[研究失敗: 資源不足]**\n**Lv.{tech_level+1}** への研究には以下の資源が必要です。\n・資金: **{cost_gold} Gold** (現在: {gold})\n・石油: **{cost_oil} L** (現在: {oil})"
                return await interaction.followup.send(msg, ephemeral=True)
            
            # 研究処理（資源を消費し、テックレベルを+1）
            c.execute("UPDATE players SET gold=gold-?, oil=oil-?, tech_level=tech_level+1 WHERE guild_id=? AND world_id=? AND user_id=?", (cost_gold, cost_oil, guild_id, world_id, user_id))
            conn.commit()
            
        msg = f"🔬 **[研究完了]**\n技術研究が成功しました！国家のテックレベルが **Lv.{tech_level+1}** に上昇しました！"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @app_commands.command(name="spy", description="他国へスパイを派遣し、情報の収集や工作を行います")
    async def cmd_spy(self, interaction: discord.Interaction, target: str):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        target_code = resolve_country_code(target)

        cost = 1500 # スパイの派遣費用
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            if not row or row[0] < cost:
                return await interaction.followup.send(f"[エラー] スパイ派遣には工作資金 **{cost} Gold** が必要です。", ephemeral=True)

            c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
            t_row = c.fetchone()
            if not t_row: 
                return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
            
            t_owner, t_defense = t_row
            if t_owner == user_id: 
                return await interaction.followup.send("[エラー] 自国にスパイは派遣できません。", ephemeral=True)

            # 確率計算 (ベース60% + テックレベル補正)
            tech_level = row[1] if row[1] else 1
            success_rate = min(95, 60 + (tech_level * 2))
            is_success = random.randint(1, 100) <= success_rate

            # 資金消費
            c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

            if is_success:
                c.execute("SELECT user_name, gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, t_owner))
                enemy = c.fetchone()
                msg = f"🕵️ **[潜入成功]**\n**{target_code}** へのスパイ潜入に成功しました！\n\n**【極秘・国家機密データ】**\n・統治者: **{enemy[0]}**\n・現在防衛力: **{t_defense}** 人\n・国家資金: **{enemy[1]}** Gold\n・石油備蓄: **{enemy[2]}** L\n・潜入成功率: {success_rate}%"
                add_world_log(guild_id, world_id, f"何者かのスパイが {target_code} に潜入したという噂が流れています。")
                add_trophy(guild_id, world_id, user_id, "凄腕の諜報員")
            else:
                msg = f"💥 **[潜入失敗]**\n**{target_code}** の防衛網に引っかかり、スパイは捕らえられました...\n工作資金 **{cost} Gold** を失いました。(成功率: {success_rate}%)"
            
            conn.commit()
            
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)


    @app_commands.command(name="propaganda", description="他国へプロパガンダ工作を行い、民衆を扇動します")
    async def cmd_propaganda(self, interaction: discord.Interaction, target: str):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        target_code = resolve_country_code(target)

        cost = 3000 # プロパガンダの費用
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()
            if not row or row[0] < cost:
                return await interaction.followup.send(f"[エラー] プロパガンダ工作には **{cost} Gold** が必要です。", ephemeral=True)

            c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
            t_row = c.fetchone()
            if not t_row: 
                return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
            
            t_owner, t_defense = t_row
            if t_owner == user_id: 
                return await interaction.followup.send("[エラー] 自国には工作を行えません。", ephemeral=True)

            # 確率計算 (ベース40% + テックレベル補正)
            tech_level = row[1] if row[1] else 1
            success_rate = min(90, 40 + (tech_level * 3))
            is_success = random.randint(1, 100) <= success_rate

            # 資金消費
            c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

            if is_success:
                # 相手の防衛力を10%〜20%減らす（最低でも10〜50の固定ダメージ保証）
                drop_percent = random.uniform(0.10, 0.20)
                drop_amount = int(t_defense * drop_percent)
                if drop_amount < 10: drop_amount = random.randint(10, 50)
                
                new_def = max(100, t_defense - drop_amount)
                c.execute("UPDATE territories SET defense=? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (new_def, guild_id, world_id, target_code))
                msg = f"📢 **[工作成功]**\n**{target_code}** の民衆を扇動し、暴動を起こさせることに成功しました！\n混乱により、現地の防衛力が **{t_defense - new_def}** 減少しました！ (現在の防衛力: {new_def})"
            else:
                msg = f"❌ **[工作失敗]**\n**{target_code}** でのプロパガンダは現地政府に見破られ、工作員は強制送還されました...\n工作資金 **{cost} Gold** を無駄にしました。"
            
            conn.commit()
            
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)


    @app_commands.command(name="ranking", description="世界ランキング(資金、領土数、軍事力など)を表示します")
    @app_commands.choices(category=[
        app_commands.Choice(name="所持資金(Gold)", value="gold"),
        app_commands.Choice(name="支配領土数", value="territory"),
        app_commands.Choice(name="総防衛力", value="defense")
    ])
    async def cmd_ranking(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        # ランキングはみんなで見たいと思うので、ephemeral=False (公開) にします
        await safe_defer(interaction, ephemeral=False) 
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id = str(interaction.guild_id)

        embed = discord.Embed(color=0xf1c40f)
        desc = ""
        
        with get_db_connection() as conn:
            c = conn.cursor()
            
            if category.value == "gold":
                embed.title = f"👑 資金ランキング [世界#{world_id}]"
                c.execute("SELECT user_name, gold FROM players WHERE guild_id=? AND world_id=? ORDER BY gold DESC LIMIT 10", (guild_id, world_id))
                for i, row in enumerate(c.fetchall()):
                    desc += f"**{i+1}位**: {row[0]} - **{row[1]}** Gold\n"
                    
            elif category.value == "territory":
                embed.title = f"🗺️ 領土数ランキング [世界#{world_id}]"
                c.execute("""
                    SELECT p.user_name, COUNT(t.iso_alpha) as t_count 
                    FROM players p JOIN territories t ON p.user_id = t.owner_id 
                    WHERE p.guild_id=? AND p.world_id=? AND t.guild_id=? AND t.world_id=? 
                    GROUP BY p.user_id ORDER BY t_count DESC LIMIT 10
                """, (guild_id, world_id, guild_id, world_id))
                for i, row in enumerate(c.fetchall()):
                    desc += f"**{i+1}位**: {row[0]} - **{row[1]}** カ国\n"
                    
            elif category.value == "defense":
                embed.title = f"🛡️ 総防衛力ランキング [世界#{world_id}]"
                c.execute("""
                    SELECT p.user_name, SUM(t.defense) as total_def 
                    FROM players p JOIN territories t ON p.user_id = t.owner_id 
                    WHERE p.guild_id=? AND p.world_id=? AND t.guild_id=? AND t.world_id=? 
                    GROUP BY p.user_id ORDER BY total_def DESC LIMIT 10
                """, (guild_id, world_id, guild_id, world_id))
                for i, row in enumerate(c.fetchall()):
                    desc += f"**{i+1}位**: {row[0]} - 兵力 **{row[1]}**\n"

        embed.description = desc if desc else "まだデータがありません。"
        await interaction.followup.send(embed=embed)


    @app_commands.command(name="trophy_show", description="獲得した実績や称号を確認し、装備します")
    async def cmd_trophy(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)

        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_name, title, wins, losses, trophy_count FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            row = c.fetchone()

            if not row:
                return await interaction.followup.send("[エラー] 国家データが見つかりません。まずは建国してください。", ephemeral=True)

            user_name, title, wins, losses, trophy_count = row
            wins = wins if wins else 0
            losses = losses if losses else 0
            total_battles = wins + losses
            win_rate = (wins / total_battles * 100) if total_battles > 0 else 0.0

        embed = discord.Embed(title=f"🏅 {user_name} の実績と称号", color=0x9b59b6)
        embed.add_field(name="現在の称号", value=f"**【 {title if title else '未設定'} 】**", inline=False)
        embed.add_field(name="戦績", value=f"⚔️ 勝利: {wins} / 敗北: {losses}\n📊 勝率: {win_rate:.1f}%", inline=True)
        embed.add_field(name="獲得トロフィー", value=f"🏆 {trophy_count if trophy_count else 0} 個", inline=True)
        
        embed.set_footer(text="※称号は /trophy_equip で変更できます。")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="trophy_equip", description="取得済みの称号を装備します")
    async def cmd_trophy_equip(self, interaction: discord.Interaction, title_name: str):
        await safe_defer(interaction, ephemeral=True)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM unlocked_trophies WHERE guild_id=? AND world_id=? AND user_id=? AND trophy_id=?", (guild_id, world_id, user_id, title_name))
            if not c.fetchone() and title_name != "未設定":
                return await interaction.followup.send(f"[エラー] 称号「{title_name}」はまだ獲得していません。", ephemeral=True)
            
            c.execute("UPDATE players SET title=? WHERE guild_id=? AND world_id=? AND user_id=?", (title_name, guild_id, world_id, user_id))
            conn.commit()
            
        await interaction.followup.send(f"🎖️ **称号変更**\n称号を **【 {title_name} 】** に変更しました！", ephemeral=True)

    @app_commands.command(name="peace", description="指定したプレイヤーに24時間の不戦協定を提案します")
    async def cmd_peace(self, interaction: discord.Interaction, target_user: discord.Member):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        
        if user_id == str(target_user.id):
            return await interaction.followup.send("[エラー] 自分自身とは協定を結べません。", ephemeral=True)
            
        view = PeaceTreatyView(guild_id, world_id, user_id, interaction.user.display_name, str(target_user.id))
        msg = f"🕊️ **不戦協定の提案**\n{interaction.user.mention} が {target_user.mention} に対して、向こう24時間の不戦協定（Peace Treaty）を提案しました。\n\n{target_user.display_name}さん、承認しますか？"
        await interaction.followup.send(content=msg, view=view)

async def setup(bot):
    await bot.add_cog(CommandsCog(bot))