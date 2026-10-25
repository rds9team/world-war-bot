import discord
from discord import app_commands
from discord.ext import commands
from main import (
    get_db_connection, safe_defer, is_slash_op_or_admin, ensure_world_context
)

class AdminCog(commands.Cog):
    @app_commands.command(name="op_setup", description="初期設定を行います（戦争用カテゴリと3つの専用チャンネルを自動作成）")
    @is_slash_op_or_admin()
    async def cmd_setup(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        category = await interaction.guild.create_category("戦争bot")
        ch1 = await category.create_text_channel("戦争bot-1")
        ch2 = await category.create_text_channel("戦争bot-2")
        ch3 = await category.create_text_channel("戦争bot-3")
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO server_channels (guild_id, world1_ch, world2_ch, world3_ch, notify_ch, notify_enabled) VALUES (?, ?, ?, ?, ?, 1)", (str(interaction.guild_id), str(ch1.id), str(ch2.id), str(ch3.id), str(ch1.id)))
            conn.commit()
            
        try:
            await ch1.send("ここは **世界1** の専用チャンネルです。\nまず `/start` で国を選んで建国し、`/command` で世界征服を始めましょう！")
            await ch2.send("ここは **世界2** の専用チャンネルです。\nまず `/start` で国を選んで建国し、`/command` で世界征服を始めましょう！")
            await ch3.send("ここは **世界3** の専用チャンネルです。\nまず `/start` で国を選んで建国し、`/command` で世界征服を始めましょう！")
        except:
            pass

        embed = discord.Embed(
            title="✅ 初期設定(セットアップ)が完了しました！",
            description="戦争Botを遊ぶための準備が整いました。",
            color=0x2ecc71
        )
        embed.add_field(name="📁 作成されたカテゴリ", value="`戦争bot`", inline=False)
        embed.add_field(name="💬 作成されたチャンネル (3つ)", value=f"{ch1.mention} (世界1用)\n{ch2.mention} (世界2用)\n{ch3.mention} (世界3用)", inline=False)
        embed.add_field(name="🔔 通知チャンネル", value=f"{ch1.mention} に設定されました。\n※`/op_reboot_setting` で後から変更可能です。", inline=False)
        embed.set_footer(text="各チャンネルで /start を入力して建国しましょう！")
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="op_npc", description="反乱軍(NPC/CPU)の自律侵攻システムのON/OFF切替")
    @app_commands.choices(mode=[app_commands.Choice(name="有効(ON)", value=1), app_commands.Choice(name="無効(OFF)", value=0)])
    @is_slash_op_or_admin()
    async def cmd_npc(self, interaction: discord.Interaction, mode: app_commands.Choice[int]):
        await safe_defer(interaction)
        guild_id = str(interaction.guild_id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO server_channels (guild_id) VALUES (?)", (guild_id,))
            c.execute("UPDATE server_channels SET npc_enabled=? WHERE guild_id=?", (mode.value, guild_id))
            conn.commit()
        await interaction.followup.send(f"[設定] 反乱軍(NPC)の侵攻システムを **{'ON (有効)' if mode.value==1 else 'OFF (無効)'}** に変更しました。")

    @app_commands.command(name="op_adj", description="隣接遠征ペナルティ(未隣接時コストアップ)のON/OFF切替")
    @is_slash_op_or_admin()
    async def cmd_adj(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        guild_id = str(interaction.guild_id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO server_channels (guild_id) VALUES (?)", (guild_id,))
            c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
            row = c.fetchone()
            new_val = 0 if (row and row[0] == 1) else 1
            c.execute("UPDATE server_channels SET adjacency_penalty=? WHERE guild_id=?", (new_val, guild_id))
            conn.commit()
        await interaction.followup.send(f"[設定] 隣接遠征ペナルティを **{'ON (有効)' if new_val==1 else 'OFF (無効)'}** に変更しました。")

    @app_commands.command(name="op_reset_interval", description="自動リセット間隔の個別変更 (0で無効化)")
    @is_slash_op_or_admin()
    async def cmd_reset_interval(self, interaction: discord.Interaction, days: int):
        await safe_defer(interaction)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO server_channels (guild_id) VALUES (?)", (str(interaction.guild_id),))
            c.execute("UPDATE server_channels SET reset_interval=? WHERE guild_id=?", (days, str(interaction.guild_id)))
            conn.commit()
        msg = f"[設定] サーバーの自動リセットを **{days}日ごと** に設定しました。" if days > 0 else "[設定] サーバーの自動リセットを **OFF (手動のみ)** に設定しました。"
        await interaction.followup.send(msg)

    @app_commands.command(name="op_reset", description="現在のアクティブな世界の全データを即時リセットします")
    @is_slash_op_or_admin()
    async def cmd_reset(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return
        with get_db_connection() as conn:
            c = conn.cursor()
            for table in ['players', 'territories', 'alliances', 'wars', 'un_members', 'un_invites', 'camps', 'camp_members', 'camp_invites']:
                c.execute(f"DELETE FROM {table} WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), world_id))
            conn.commit()
        await interaction.followup.send(f"[完了] [世界#{world_id}] の全データを手動リセットしました。新たな歴史の始まりです。")

    @app_commands.command(name="op_op_setting", description="指定ユーザーにOP権限を付与または剥奪します")
    @app_commands.choices(mode=[app_commands.Choice(name="権限付与(ON)", value=1), app_commands.Choice(name="権限剥奪(OFF)", value=0)])
    @is_slash_op_or_admin()
    async def cmd_op_setting(self, interaction: discord.Interaction, target: discord.Member, mode: app_commands.Choice[int]):
        await safe_defer(interaction)
        add_op = (mode.value == 1)
        with get_db_connection() as conn:
            c = conn.cursor()
            if add_op:
                c.execute("INSERT OR IGNORE INTO server_ops (guild_id, user_id) VALUES (?, ?)", (str(interaction.guild_id), str(target.id)))
                msg = f"[成功] {target.mention} に管理・OP権限を付与しました。"
            else:
                c.execute("DELETE FROM server_ops WHERE guild_id=? AND user_id=?", (str(interaction.guild_id), str(target.id)))
                msg = f"[成功] {target.mention} の管理・OP権限を剥奪しました。"
            conn.commit()
        await interaction.followup.send(msg)

    @app_commands.command(name="op_oil_setting", description="石油消費システムの切替を行います")
    @app_commands.choices(world=[app_commands.Choice(name="全サーバー(0)", value=0), app_commands.Choice(name="世界 #1", value=1), app_commands.Choice(name="世界 #2", value=2), app_commands.Choice(name="世界 #3", value=3)])
    @app_commands.choices(mode=[app_commands.Choice(name="有効(ON)", value=1), app_commands.Choice(name="無効(OFF)", value=0)])
    @is_slash_op_or_admin()
    async def cmd_oil_setting(self, interaction: discord.Interaction, world: app_commands.Choice[int], mode: app_commands.Choice[int]):
        await safe_defer(interaction)
        val = mode.value; w_id = world.value
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO server_channels (guild_id) VALUES (?)", (str(interaction.guild_id),))
            if w_id == 0:
                c.execute("UPDATE server_channels SET oil_enabled_w1=?, oil_enabled_w2=?, oil_enabled_w3=? WHERE guild_id=?", (val, val, val, str(interaction.guild_id)))
                msg = f"[設定] サーバー全体の石油システムを **{'ON' if val==1 else 'OFF'}** に設定しました。"
            else:
                c.execute(f"UPDATE server_channels SET oil_enabled_w{w_id}=? WHERE guild_id=?", (val, str(interaction.guild_id)))
                msg = f"[設定] 世界 #{w_id} の石油システムを **{'ON' if val==1 else 'OFF'}** に設定しました。"
            conn.commit()
        await interaction.followup.send(msg)

    @app_commands.command(name="op_channel_setting", description="既存のチャンネルを各Worldに手動紐付けします")
    @is_slash_op_or_admin()
    async def cmd_channel_setting(self, interaction: discord.Interaction, world1: discord.TextChannel, world2: discord.TextChannel, world3: discord.TextChannel):
        await safe_defer(interaction)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO server_channels (guild_id, world1_ch, world2_ch, world3_ch, notify_ch, notify_enabled) VALUES (?, ?, ?, ?, COALESCE((SELECT notify_ch FROM server_channels WHERE guild_id=?), ?), 1)", (str(interaction.guild_id), str(world1.id), str(world2.id), str(world3.id), str(interaction.guild_id), str(world1.id)))
            conn.commit()
        await interaction.followup.send("[完了] 各Worldの専用チャンネルを手動で設定しました。")

    @app_commands.command(name="op_reboot_setting", description="定時給付やワイプの通知先チャンネルとON/OFFを設定します")
    @app_commands.choices(mode=[app_commands.Choice(name="有効(ON)", value=1), app_commands.Choice(name="無効(OFF)", value=0)])
    @is_slash_op_or_admin()
    async def cmd_reboot_setting(self, interaction: discord.Interaction, notify_channel: discord.TextChannel, mode: app_commands.Choice[int]):
        await safe_defer(interaction)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO server_channels (guild_id) VALUES (?)", (str(interaction.guild_id),))
            c.execute("UPDATE server_channels SET notify_ch=?, notify_enabled=? WHERE guild_id=?", (str(notify_channel.id), mode.value, str(interaction.guild_id)))
            conn.commit()
        await interaction.followup.send(f"[設定] 通知設定を **{'ON' if mode.value==1 else 'OFF'}** に変更し、{notify_channel.mention} に設定しました。")

async def setup(bot):
    await bot.add_cog(AdminCog(bot))