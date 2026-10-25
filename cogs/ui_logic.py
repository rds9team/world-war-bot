import discord
from discord import app_commands
import random
import datetime
import asyncio
import os

from main import (
    get_db_connection, safe_defer, send_dm_fallback, ensure_world_context, 
    is_oil_enabled, resolve_country_code, is_allied, is_at_war, 
    check_and_create_user, _generate_current_map_sync, get_promo_and_tip, 
    VALID_CODES, ADJACENCY_GRAPH, DEFAULT_DEFENSE, BASE_INCOME, TERRITORY_YIELD,
    add_world_log, add_trophy, is_peace_treaty_active
)

work_cooldowns = {}

async def my_countries_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return []
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        choices = [row[0] for row in c.fetchall() if current.lower() in row[0].lower()]
        return [app_commands.Choice(name=f"領土: {code}", value=code) for code in choices[:25]]

# ==============================================================================
# UI・画面パーツ (Views / Modals)
# ==============================================================================
class StatsView(discord.ui.View):
    def __init__(self, author_id: int, user_data: dict):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user_data = user_data

    def generate_embed(self, category: str):
        data = self.user_data
        embed = discord.Embed(title=f"📊 {data['user_name']} ({data['country_name']}) の国家データ", color=0xf1c40f)
        
        if category == "economy":
            embed.description = "**💰 経済・内政**"
            embed.add_field(name="保有資金 (Gold)", value=f"{data['money']}", inline=True)
            embed.add_field(name="備蓄石油 (Oil)", value=f"{data['oil']} L", inline=True)
            embed.add_field(name="戦債 (借金)", value=f"{data['debt']}", inline=True)
            embed.add_field(name="総投資額", value=f"{data['invest']}", inline=True)
            embed.add_field(name="テックレベル", value=f"Lv. {data['tech_level']}", inline=True)
        elif category == "military":
            embed.description = "**⚔️ 軍事・領土**"
            embed.add_field(name="支配領土数", value=f"{data['territory']} カ国", inline=True)
            embed.add_field(name="総防衛力", value=f"{data['defense']} 人", inline=True)
            embed.add_field(name="戦績", value=f"勝: {data['wins']} / 敗: {data['losses']}", inline=True)
        elif category == "profile":
            embed.description = "**🏅 プロフィール・実績**"
            embed.add_field(name="称号", value=f"{data['title']}", inline=True)
            embed.add_field(name="トロフィー", value=f"{data['trophy_count']} 個", inline=True)
            
        return embed

    @discord.ui.button(label="経済・内政", style=discord.ButtonStyle.success)
    async def btn_eco(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("economy"), view=self)

    @discord.ui.button(label="軍事・領土", style=discord.ButtonStyle.danger)
    async def btn_mil(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("military"), view=self)

    @discord.ui.button(label="実績・プロフ", style=discord.ButtonStyle.primary)
    async def btn_prof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("操作できません。", ephemeral=True)
        await interaction.response.edit_message(embed=self.generate_embed("profile"), view=self)

class InvestModal(discord.ui.Modal, title="国家予算の投資"):
    amount_input = discord.ui.TextInput(label="投資額 (Gold)", placeholder="例: 1000", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        try: amount = int(self.amount_input.value)
        except ValueError: return await interaction.response.send_message("[エラー] 数字を入力してください。", ephemeral=True)
        await run_invest(interaction, amount)

class SpyModal(discord.ui.Modal, title="スパイの派遣 (費用: 1500 Gold)"):
    target_input = discord.ui.TextInput(label="潜入先の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_spy(interaction, self.target_input.value)

class PropagandaModal(discord.ui.Modal, title="プロパガンダ工作 (費用: 3000 Gold)"):
    target_input = discord.ui.TextInput(label="工作先の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_propaganda(interaction, self.target_input.value)
class OilImportView(discord.ui.View):
    def __init__(self, guild_id, world_id, buyer_id, seller_id, amount, price):
        super().__init__(timeout=86400)
        self.guild_id = guild_id; self.world_id = world_id; self.buyer_id = buyer_id
        self.seller_id = seller_id; self.amount = amount; self.price = price

    @discord.ui.button(label="許可する", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.seller_id: return await interaction.response.send_message("[エラー] 権限がありません。", ephemeral=True)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (self.guild_id, self.world_id, self.seller_id))
            seller_row = c.fetchone()
            if not seller_row or seller_row[0] < self.amount: return await interaction.response.send_message("[エラー] オイル不足です。", ephemeral=True)
            c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (self.guild_id, self.world_id, self.buyer_id))
            buyer_row = c.fetchone()
            if not buyer_row or buyer_row[0] < self.price: return await interaction.response.send_message("[エラー] 相手の資金不足です。", ephemeral=True)

            c.execute("UPDATE players SET oil=oil-?, gold=gold+? WHERE guild_id=? AND world_id=? AND user_id=?", (self.amount, self.price, self.guild_id, self.world_id, self.seller_id))
            c.execute("UPDATE players SET oil=oil+?, gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (self.amount, self.price, self.guild_id, self.world_id, self.buyer_id))
            conn.commit()

        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content=f"[取引成立 / 世界#{self.world_id}]\n<@{self.buyer_id}> へ **{self.amount} L** を輸出し **{self.price} Gold** を受け取りました。", view=self)

        guild = interaction.client.get_guild(int(self.guild_id))
        if guild:
            buyer_member = guild.get_member(int(self.buyer_id))
            if buyer_member:
                try: await buyer_member.send(f"[取引完了 / 世界#{self.world_id}]\n<@{self.seller_id}> が申請を許可しました。\n**{self.amount} L** を獲得し、**{self.price} Gold** 支払いました。")
                except: pass

    @discord.ui.button(label="拒否する", style=discord.ButtonStyle.danger)
    async def btn_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.seller_id: return await interaction.response.send_message("[エラー] 権限がありません。", ephemeral=True)
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content=f"[拒否 / 世界#{self.world_id}] 取引を拒否しました。", view=self)

class DiplomacyUserSelect(discord.ui.UserSelect):
    def __init__(self, action: str, world_id: int):
        self.action = action; self.world_id = world_id
        super().__init__(placeholder="対象のプレイヤーを選択してください...")

    async def callback(self, interaction: discord.Interaction):
        await safe_defer(interaction) 
        guild_id, user_id, target_id = str(interaction.guild_id), str(interaction.user.id), str(self.values[0].id)
        w_id = self.world_id; target = self.values[0]

        if user_id == target_id: return await interaction.followup.send("[エラー] 自分は選択できません。", ephemeral=True)
        if target.bot: return await interaction.followup.send("[エラー] Botは選択できません。", ephemeral=True)

        target_member = interaction.guild.get_member(int(target_id))

        with get_db_connection() as conn:
            c = conn.cursor()
            is_ally = is_allied(guild_id, w_id, user_id, target_id)

            if self.action == "war":
                if is_ally: return await interaction.followup.send("[エラー] 同盟国には宣戦布告できません。", ephemeral=True)
                view = discord.ui.View(timeout=86400)
                btn_accept = discord.ui.Button(label="受諾する", style=discord.ButtonStyle.danger)
                btn_reject = discord.ui.Button(label="拒否する", style=discord.ButtonStyle.success)

                async def accept_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    with get_db_connection() as conn2:
                        c2 = conn2.cursor()
                        c2.execute("INSERT OR IGNORE INTO wars (guild_id, world_id, attacker_id, defender_id) VALUES (?, ?, ?, ?)", (guild_id, w_id, user_id, target_id))
                        conn2.commit()
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[宣戦布告 受諾 / 世界#{w_id}]\n<@{target_id}> と <@{user_id}> は正式に戦争状態に突入しました！", view=view)

                async def reject_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[宣戦布告 拒否 / 世界#{w_id}]\n<@{target_id}> は宣戦布告を拒否しました。\n<@{user_id}> は強行軍（奇襲）を仕掛けるしかありません。", view=view)

                btn_accept.callback = accept_callback; btn_reject.callback = reject_callback
                view.add_item(btn_accept); view.add_item(btn_reject)

                content = f"**[宣戦布告の使者 / 世界#{w_id}]**\n<@{user_id}> から宣戦布告の使者が到着しました！\n受諾するか拒否するか選択してください。\n※拒否しても交渉決裂となり、相手は強行軍(奇襲)を仕掛けることができます。"
                if target_member: await send_dm_fallback(target_member, interaction.channel, content, view)
                else: await interaction.channel.send(f"<@{target_id}> {content}", view=view)
                await interaction.followup.send("[完了] 宣戦布告の使者を派遣しました。", ephemeral=True)

            elif self.action == "alliance_invite":
                if is_ally: return await interaction.followup.send("[エラー] すでに同盟を結んでいます。", ephemeral=True)
                view = discord.ui.View(timeout=86400)
                btn_accept = discord.ui.Button(label="承認する", style=discord.ButtonStyle.success)
                async def accept_callback(i: discord.Interaction):
                    if str(i.user.id) != target_id: return await i.response.send_message("[エラー] 権限がありません。", ephemeral=True)
                    with get_db_connection() as conn2:
                        c2 = conn2.cursor()
                        c2.execute("INSERT OR IGNORE INTO alliances (guild_id, world_id, user_a, user_b) VALUES (?, ?, ?, ?)", (guild_id, w_id, user_id, target_id))
                        c2.execute("DELETE FROM wars WHERE guild_id=? AND world_id=? AND ((attacker_id=? AND defender_id=?) OR (attacker_id=? AND defender_id=?))", (guild_id, w_id, user_id, target_id, target_id, user_id))
                        conn2.commit()
                    for child in view.children: child.disabled = True
                    await i.response.edit_message(content=f"[同盟成立 / 世界#{w_id}]\n<@{user_id}> と <@{target_id}> が軍事同盟を結びました！", view=view)
                btn_accept.callback = accept_callback
                view.add_item(btn_accept)
                
                content = f"**[軍事同盟の提案 / 世界#{w_id}]**\n<@{user_id}> から軍事同盟の提案が届きました！\n承認を押して同盟を結びますか？"
                if target_member: await send_dm_fallback(target_member, interaction.channel, content, view)
                else: await interaction.channel.send(f"<@{target_id}> {content}", view=view)
                await interaction.followup.send("[完了] 同盟の提案を送信しました。", ephemeral=True)

            elif self.action == "alliance_cancel":
                if not is_ally: return await interaction.followup.send("[エラー] 同盟を結んでいません。", ephemeral=True)
                c.execute("DELETE FROM alliances WHERE guild_id=? AND world_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))", (guild_id, w_id, user_id, target_id, target_id, user_id))
                conn.commit()
                await interaction.channel.send(f"[同盟破棄 / 世界#{w_id}] <@{user_id}> が <@{target_id}> との同盟を破棄しました。")
                await interaction.followup.send("[完了] 同盟を破棄しました。", ephemeral=True)

class DiplomacyActionSelect(discord.ui.Select):
    def __init__(self, world_id: int):
        self.world_id = world_id
        options = [
            discord.SelectOption(label="宣戦布告", value="war"),
            discord.SelectOption(label="同盟の提案", value="alliance_invite"),
            discord.SelectOption(label="同盟の破棄", value="alliance_cancel"),
            discord.SelectOption(label="同盟国一覧", value="alliance_list"),
            discord.SelectOption(label="国連加盟国一覧", value="un_list"),
            discord.SelectOption(label="設立陣営一覧", value="camp_list")
        ]
        super().__init__(placeholder="実行する外交アクションを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "alliance_list":
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT user_a, user_b FROM alliances WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), self.world_id))
                rows = c.fetchall()
            if not rows: return await interaction.response.send_message(f"[世界#{self.world_id}] 結ばれている同盟はありません。", ephemeral=True)
            embed = discord.Embed(title=f"締結済みの軍事同盟 [世界#{self.world_id}]", description="\n".join([f"・ <@{a}> ＆ <@{b}>" for a, b in rows]), color=0x3498db)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        elif val == "un_list": await run_un_list(interaction)
        elif val == "camp_list": await run_camp_list(interaction)
        else:
            view = discord.ui.View(timeout=60)
            view.add_item(DiplomacyUserSelect(val, self.world_id))
            await interaction.response.send_message("👉 次に対象となるプレイヤーを選択してください:", view=view, ephemeral=True)

class CountryManageSelect(discord.ui.Select):
    def __init__(self, territories, world_id, user_id):
        self.world_id = world_id; self.user_id = user_id
        options = [discord.SelectOption(label=t[0], description=f"防衛力: {t[1]}") for t in territories[:25]]
        super().__init__(placeholder="管理する領土を選択してください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_iso = self.values[0]
        view = discord.ui.View(timeout=60)
        btn_abandon = discord.ui.Button(label="所有権を放棄する", style=discord.ButtonStyle.danger)
        async def abandon_callback(i: discord.Interaction):
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("DELETE FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=? AND owner_id=?", (str(i.guild_id), self.world_id, selected_iso, self.user_id))
                conn.commit()
            for child in view.children: child.disabled = True
            await i.response.edit_message(content=f"[完了] **{selected_iso}** の所有権を放棄しました。", view=view)
        btn_abandon.callback = abandon_callback
        view.add_item(btn_abandon)
        await interaction.response.edit_message(content=f"**{selected_iso}** の管理メニュー", view=view)

class CountryManageMainView(discord.ui.View):
    def __init__(self, territories, world_id, user_id):
        super().__init__(timeout=60)
        self.add_item(CountryManageSelect(territories, world_id, user_id))

class HelpView(discord.ui.View):
    def __init__(self): 
        super().__init__(timeout=None)

    def generate_embed(self, page_id: str):
        embed = discord.Embed(title="全世界戦争Bot ガイドブック", color=0x3498db)
        
        if page_id == "basic":
            embed.description = "**🔰 ゲームの始め方＆基本ルール**\n国を奪い合い、世界の覇権を握るシミュレーションゲームです。"
            embed.add_field(name="1️⃣ まずは建国 (/start)", value="`/start` で好きな国（日本やアメリカ等）を本拠地として選び、**建国祝賀ボーナス（資金+石油）**を受け取りましょう！", inline=False)
            embed.add_field(name="2️⃣ かんたん操作 (/command)", value="`/command` を開くと**国家司令部ダッシュボード**が展開され、ボタン操作で労働・防衛・出撃がすべて完結します！", inline=False)
            embed.add_field(name="3️⃣ ソロでも遊べる反乱軍システム", value="定期的にマップ上の空き地を「🚩 反乱軍(CPU)」が占拠します。撃退すると**討伐ボーナス資金(+500 Gold)**を獲得できます！", inline=False)
            embed.add_field(name="4️⃣ 定時給付と資源", value="毎日(7:00 / 19:00 JST)に基本給(1000G)・税収・石油配給があります。\n資金が尽きたら `/war_bonds` で国庫から5000G借りられます(給付から天引き)。", inline=False)
            
        elif page_id == "war":
            embed.description = "**戦争と内政**"
            embed.add_field(name="侵攻作戦", value="`/command` の「⚔️ 侵攻」または `/attack` で他国・反乱軍・空き地を攻めます。**入力した資金が部隊の戦力**になります。\n宣戦布告なしの奇襲や、遠い国への攻撃は、実質的な攻撃力がペナルティで減少します。", inline=False)
            embed.add_field(name="防衛と大国ボーナス", value="`/defend` または `/command` の「🛡️ 防衛」で領土の防衛力を上げます。\n**領土を3つ以上持つ大国**は、防衛費が割引(0.9倍)され、より効率的に国を守ることができます。", inline=False)
            
        elif page_id == "diplomacy":
            embed.description = "**外交と貿易**"
            embed.add_field(name="宣戦布告と同盟", value="`/gui` から行います。宣戦布告を断られても、相手に奇襲を仕掛けることができます。\n同盟を結ぶと相互不可侵となり、攻撃を受けた際にDM通知が飛びます。", inline=False)
            embed.add_field(name="貿易", value="`/import_oil` で他プレイヤーからオイルを購入したり、`/convert_resource` でシステムと直接資金・オイルを交換できます。", inline=False)
            
        elif page_id == "system":
            embed.description = "**システムとアップデート**"
            embed.add_field(name="バージョン一覧", value="`/version` : 閲覧可能なアップデート一覧（最新10件）を確認できます。", inline=False)
            embed.add_field(name="アプデ詳細確認", value="`/update` : 最新のアップデート情報を表示します。\n`/update [バージョン]` : 指定した過去バージョンの詳細を表示します。", inline=False)
            embed.add_field(name="個人設定", value="`/user_setting` : 確認ダイアログのON/OFFなど、各種表示設定を変更できます。", inline=False)

        elif page_id == "admin":
            embed.description = "**管理者向け機能**"
            embed.add_field(name="初期セットアップ", value="初めに `/op_setup` で専用チャンネルを一括作成してください。\nその他の設定もすべて `/op_` から始まるコマンドで操作可能です。", inline=False)
            
        return embed

    @discord.ui.button(label="基本ルール", style=discord.ButtonStyle.primary, custom_id="help_basic")
    async def btn_basic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("basic"), view=self)

    @discord.ui.button(label="戦争と内政", style=discord.ButtonStyle.secondary, custom_id="help_war")
    async def btn_war(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("war"), view=self)

    @discord.ui.button(label="外交と貿易", style=discord.ButtonStyle.secondary, custom_id="help_dip")
    async def btn_dip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("diplomacy"), view=self)

    @discord.ui.button(label="システム・更新", style=discord.ButtonStyle.success, custom_id="help_sys")
    async def btn_sys(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("system"), view=self)

    @discord.ui.button(label="管理者向け", style=discord.ButtonStyle.danger, custom_id="help_admin")
    async def btn_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.generate_embed("admin"), view=self)

RECOMMENDED_STARTER_COUNTRIES = [
    ("JPN", "🇯🇵 日本 (JPN)", "アジアの島国。高い初期収益と守りやすい防衛地形"),
    ("USA", "🇺🇸 アメリカ (USA)", "巨大な経済力と高収益を誇る超大国"),
    ("GBR", "🇬🇧 イギリス (GBR)", "ヨーロッパの海洋国家。防衛しやすい島国"),
    ("FRA", "🇫🇷 フランス (FRA)", "欧州の中心に位置し、隣接国が多い好立地"),
    ("DEU", "🇩🇪 ドイツ (DEU)", "欧州の工業大国。内陸の要所"),
    ("AUS", "🇦🇺 オーストラリア (AUS)", "南半球の海洋孤島。他国から侵略されにくい"),
    ("BRA", "🇧🇷 ブラジル (BRA)", "南米の覇者。広大な領土と安定した収益"),
    ("IND", "🇮🇳 インド (IND)", "アジアの成長拠点。高い人口と経済力"),
    ("KOR", "🇰🇷 韓国 (KOR)", "東アジアの要衝。日本・中国に隣接"),
    ("EGY", "🇪🇬 エジプト (EGY)", "中東・アフリカを結ぶ交通の要衝"),
]

class StartCountrySelect(discord.ui.Select):
    def __init__(self, world_id: int):
        self.world_id = world_id
        options = [
            discord.SelectOption(label=label, value=code, description=desc[:100])
            for code, label, desc in RECOMMENDED_STARTER_COUNTRIES
        ]
        super().__init__(placeholder="🚩 あなたの最初の領土（首都）を選択してください...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        guild_id, user_id, user_name = str(interaction.guild_id), str(interaction.user.id), interaction.user.display_name
        chosen_code = self.values[0]

        with get_db_connection() as conn:
            c = conn.cursor()
            check_and_create_user(c, guild_id, self.world_id, user_id, user_name)
            
            c.execute("SELECT owner_id FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, self.world_id, chosen_code))
            t_row = c.fetchone()
            
            if t_row and t_row[0] != "NPC_REBELS" and t_row[0] != user_id:
                return await interaction.followup.send(f"[エラー] **{chosen_code}** は既に他のプレイヤーが支配しています！別の国を選択してください。", ephemeral=True)

            c.execute("INSERT OR REPLACE INTO territories (guild_id, world_id, iso_alpha, owner_id, defense) VALUES (?, ?, ?, ?, ?)",
                      (guild_id, self.world_id, chosen_code, user_id, 300))
            
            c.execute("UPDATE players SET gold=gold+2000, oil=oil+5000, main_country=?, tutorial_done=1 WHERE guild_id=? AND world_id=? AND user_id=?",
                      (chosen_code, guild_id, self.world_id, user_id))
            
            add_world_log(guild_id, self.world_id, f"🎉 新国家誕生！ {user_name} が 【{chosen_code}】 を本拠地として建国しました！")
            add_trophy(guild_id, self.world_id, user_id, "新進気鋭の征服者")
            conn.commit()

        embed = discord.Embed(
            title="🎉 建国完了！ 新たな歴史の始まりです",
            description=f"おめでとうございます！ **{user_name}** の本拠地として **【{chosen_code}】** が正式に認められました！\n\n"
                        "💰 **建国祝賀金**: `+2,000 Gold`\n"
                        "🛢️ **備蓄石油**: `+5,000 L`\n"
                        "🛡️ **初期防衛力**: `300`\n\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "**【次のステップ】**\n"
                        "1. `/command` を入力して**国家司令部（ダッシュボード）**を開きましょう！\n"
                        "2. ボタン操作で労働・防衛・出撃を試してみましょう！",
            color=0x2ecc71
        )
        await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

class StartCountryView(discord.ui.View):
    def __init__(self, world_id: int):
        super().__init__(timeout=180)
        self.add_item(StartCountrySelect(world_id))

class AttackCandidateSelect(discord.ui.Select):
    def __init__(self, candidates: list[tuple[str, str, str]], active_world: int):
        self.active_world = active_world
        options = [
            discord.SelectOption(label=f"🎯 {code}", value=code, description=f"{owner_info} (防衛:{defense})")
            for code, owner_info, defense in candidates[:25]
        ]
        super().__init__(placeholder="⚔️ 侵攻目標の国を選択してください...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        target_code = self.values[0]
        await interaction.response.send_modal(TroopModal(target_code, self.active_world))

class AttackCandidateView(discord.ui.View):
    def __init__(self, candidates: list[tuple[str, str, str]], active_world: int):
        super().__init__(timeout=90)
        self.add_item(AttackCandidateSelect(candidates, active_world))

def build_dashboard_embed(guild_id: str, world_id: int, user_id: str, user_name: str) -> tuple[discord.Embed, list[str]]:
    with get_db_connection() as conn:
        c = conn.cursor()
        check_and_create_user(c, guild_id, world_id, user_id, user_name)
        c.execute("SELECT gold, oil, main_country, debt, tech_level, title FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        p_row = c.fetchone()
        gold, oil, main_country, debt, tech_level, title = p_row if p_row else (1000, 8000, "未設定", 0, 1, "未設定")

        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        territories = c.fetchall()
        my_lands = [t[0] for t in territories]
        total_def = sum(t[1] for t in territories)

    embed = discord.Embed(
        title=f"👑 国家司令部ダッシュボード [世界#{world_id}]",
        description=f"国家指導者: **{user_name}** | 称号: `{title}`",
        color=0x3498db
    )
    lands_str = ", ".join(my_lands) if my_lands else "なし（※下のボタンまたは /start で建国可能）"
    embed.add_field(name="🏰 領土状況", value=f"**本拠地:** {main_country} | **領土数:** {len(my_lands)} カ国\n**総防衛力:** {total_def:,}\n`{lands_str[:250]}`", inline=False)
    embed.add_field(name="💰 国庫・資源", value=f"**資金:** {gold:,} Gold\n**石油:** {oil:,} L\n**戦債(借金):** {debt:,} Gold", inline=True)
    embed.add_field(name="🔬 国家機能", value=f"**テックLv:** {tech_level}\n**定時給付:** 7:00 / 19:00", inline=True)

    advise = []
    if not my_lands:
        advise.append("🚩 **まずは建国！** `/start` で本拠地を選び、建国祝賀金を受け取りましょう！")
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
        cd_key = (guild_id, world_id, user_id)
        can_work = True
        if cd_key in work_cooldowns:
            elapsed = (now - work_cooldowns[cd_key]).total_seconds()
            if elapsed < 3600: can_work = False
        if can_work:
            advise.append("💼 **労働の準備完了！** 「💼 労働」ボタンで資金と石油を即補給できます！")
        if total_def < 200 * len(my_lands):
            advise.append("🛡️ **防衛が手薄です！** 「🛡️ 防衛」ボタンで守りを固めましょう！")
        if gold >= 1000:
            advise.append("⚔️ **進軍可能！** 「⚔️ 侵攻」ボタンから隣接する国を攻めて領土を広げましょう！")

    if not advise:
        advise.append("🕊️ 平和な統治が続いています。技術研究や他国との外交を進めましょう！")

    embed.add_field(name="💡 指導者への助言（おすすめアクション）", value="\n".join(advise), inline=False)
    embed.set_footer(text="下のボタンを押すだけで簡単に操作できます！")
    return embed, my_lands

class CommandGUIView(discord.ui.View):
    def __init__(self, world_id: int = 1, my_lands: list[str] = None):
        super().__init__(timeout=None)
        self.world_id = world_id
        self.my_lands = my_lands or []

    @discord.ui.button(label="⚔️ 侵攻作戦 (Attack)", style=discord.ButtonStyle.danger, row=0)
    async def btn_attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        candidates = []
        with get_db_connection() as conn:
            c = conn.cursor()
            target_pool = set()
            for land in self.my_lands:
                target_pool.update(ADJACENCY_GRAPH.get(land, set()))
            target_pool.difference_update(self.my_lands)
            
            if not target_pool:
                c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id='NPC_REBELS' LIMIT 10", (guild_id, self.world_id))
                target_pool.update(r[0] for r in c.fetchall())
                target_pool.update(["JPN", "KOR", "CHN", "USA", "GBR", "FRA", "DEU", "IND", "AUS", "BRA"])
                target_pool.difference_update(self.my_lands)

            for code in list(target_pool)[:25]:
                c.execute("SELECT t.defense, p.user_name, t.owner_id FROM territories t LEFT JOIN players p ON t.owner_id=p.user_id AND t.world_id=p.world_id AND t.guild_id=p.guild_id WHERE t.guild_id=? AND t.world_id=? AND t.iso_alpha=?", (guild_id, self.world_id, code))
                row = c.fetchone()
                if row:
                    owner_text = "🚩 反乱軍(CPU)" if row[2] == "NPC_REBELS" else f"統治者: {row[1]}"
                    candidates.append((code, owner_text, str(row[0])))
                else:
                    candidates.append((code, "空き地 (未占領)", str(DEFAULT_DEFENSE)))

        if not candidates:
            return await interaction.response.send_modal(AttackTargetModal())
        
        await interaction.response.send_message("侵攻する目標国を選択してください（選択後、投入資金を設定します）:", view=AttackCandidateView(candidates, self.world_id), ephemeral=True)

    @discord.ui.button(label="🛡️ 防衛強化 (Defend)", style=discord.ButtonStyle.success, row=0)
    async def btn_defend(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, self.world_id, user_id))
            territories = c.fetchall()
        if not territories:
            return await interaction.response.send_message("[エラー] 所有している領土がありません。まずは `/start` で建国するか、侵攻して領土を獲得しましょう！", ephemeral=True)
        await interaction.response.send_message("防衛費を投資する領土を選択してください:", view=DefendView(territories, self.world_id), ephemeral=True)

    @discord.ui.button(label="💼 労働 (Work)", style=discord.ButtonStyle.primary, row=0)
    async def btn_work(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
        now = datetime.datetime.now(datetime.timezone.utc)
        key = (guild_id, self.world_id, user_id)
        if key in work_cooldowns:
            elapsed = (now - work_cooldowns[key]).total_seconds()
            if elapsed < 3600:
                minutes, seconds = divmod(int(3600 - elapsed), 60)
                return await interaction.followup.send(f"労働の準備中です。あと **{minutes}分 {seconds}秒** お待ちください。", ephemeral=True)
                
        work_cooldowns[key] = now
        earned_gold = random.randint(300, 800)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            check_and_create_user(c, guild_id, self.world_id, user_id, interaction.user.display_name)
            c.execute("SELECT 1 FROM un_members WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, self.world_id, user_id))
            is_un = c.fetchone()
            
            if is_un:
                earned_gold = int(earned_gold * 1.5)
                earned_oil = random.randint(75, 225)
                bonus_msg = f"\n[国連ボーナス] 追加資金と石油(**{earned_oil} L**)を採掘しました！"
            else:
                earned_oil = random.randint(50, 150)
                bonus_msg = f"\n[通常採掘] 石油(**{earned_oil} L**)を採掘しました！"
                
            c.execute("UPDATE players SET gold=gold+?, oil=oil+? WHERE guild_id=? AND world_id=? AND user_id=?", (earned_gold, earned_oil, guild_id, self.world_id, user_id))
            c.execute("SELECT gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, self.world_id, user_id))
            current_gold, current_oil = c.fetchone()
            conn.commit()
            
        msg = f"🎉 労働完了！ [世界#{self.world_id}] で `{earned_gold}` Gold を稼ぎました！{bonus_msg}\n現在の所持金: **{current_gold:,} Gold** / 石油: **{current_oil:,} L**"
        await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

    @discord.ui.button(label="🗺️ 戦況マップ (Map)", style=discord.ButtonStyle.secondary, row=1)
    async def btn_map(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        guild_id = str(interaction.guild_id)
        map_file, occupied_lands = await asyncio.to_thread(_generate_current_map_sync, guild_id, self.world_id)
        embed = discord.Embed(title=f"🗺️ 世界情勢マップ [世界#{self.world_id}]", color=0x3498db)
        owner_dict = {}
        for iso, _, _, _, u_name in occupied_lands: owner_dict.setdefault(u_name, []).append(iso)
        desc = "\n".join([f"**{u_name}**: {', '.join(lands)}" for u_name, lands in owner_dict.items()])
        embed.description = "【現在の支配状況】\n" + (desc[:3500] if desc else "支配されている領土はありません。")
        if map_file: await interaction.followup.send(content=get_promo_and_tip(), embed=embed.set_image(url="attachment://war_map.png"), file=map_file, ephemeral=True)
        else: await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

    @discord.ui.button(label="📖 遊び方ガイド (Help)", style=discord.ButtonStyle.secondary, row=1)
    async def btn_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction, ephemeral=True)
        view = HelpView()
        await interaction.followup.send(embed=view.generate_embed("basic"), view=view, ephemeral=True)

    @discord.ui.select(placeholder="その他の上級アクション・外交メニュー...", row=2, options=[
        discord.SelectOption(label="国家への投資 (Invest)", value="invest", description="資金を投資して国力を高める"),
        discord.SelectOption(label="技術研究 (Research)", value="research", description="技術レベルを上げ、工作の成功率をアップ"),
        discord.SelectOption(label="スパイ派遣 (Spy)", value="spy", description="他国へ潜入し、情報を抜き取る"),
        discord.SelectOption(label="プロパガンダ (Propaganda)", value="propaganda", description="他国の防衛力を低下させる"),
        discord.SelectOption(label="外交手続き (GUI)", value="gui", description="宣戦布告や同盟の申請/破棄を行う"),
        discord.SelectOption(label="領土の管理", value="country_management", description="所有している領土の所有権放棄メニュー"),
        discord.SelectOption(label="詳細ステータス", value="status", description="詳細な国家データを確認"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        val = select.values[0]
        if val == "invest": await interaction.response.send_modal(InvestModal())
        elif val == "spy": await interaction.response.send_modal(SpyModal())
        elif val == "propaganda": await interaction.response.send_modal(PropagandaModal())
        else:
            await interaction.response.edit_message(view=self)
            if val == "status": await run_status(interaction)
            elif val == "gui": await run_diplomacy(interaction)
            elif val == "country_management": await run_country_management(interaction)
            elif val == "research": await run_research(interaction)

async def run_start_wizard(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id, user_name = str(interaction.guild_id), str(interaction.user.id), interaction.user.display_name
    with get_db_connection() as conn:
        c = conn.cursor()
        check_and_create_user(c, guild_id, world_id, user_id, user_name)
        c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        lands = [r[0] for r in c.fetchall()]
        if lands:
            embed = discord.Embed(
                title="🏰 すでに建国済みです！",
                description=f"あなたは既に **{len(lands)}** カ国（本拠地・領土: {', '.join(lands)}）を統治しています！\n\n"
                            "国家の運営や他国への侵攻は `/command` からワンタップで行えます！",
                color=0x3498db
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

    embed = discord.Embed(
        title="🚩 国家建国ウィザード [世界#" + str(world_id) + "]",
        description="世界の覇権を目指す新たな国家指導者よ、ようこそ！\n\n"
                    "下のメニューから、あなたの**本拠地（首都）となる国**を1つ選択してください。\n"
                    "建国と同時に、国庫に**祝賀ボーナス資金と石油**が支給されます！",
        color=0x2ecc71
    )
    embed.add_field(name="🎁 建国ボーナス", value="・資金: **+2,000 Gold**\n・石油: **+5,000 L**\n・首都防衛力: **300人**", inline=False)
    await interaction.followup.send(embed=embed, view=StartCountryView(world_id), ephemeral=True)


class AttackConfirmView(discord.ui.View):
    def __init__(self, target_code: str, active_world: int, total_cost: int, actual_power: int, warning_text: str):
        super().__init__(timeout=60)
        self.target_code = target_code; self.active_world = active_world
        self.total_cost = total_cost; self.actual_power = actual_power; self.warning_text = warning_text

    @discord.ui.button(label="この内容で進軍する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content="部隊を出撃させています...", view=self)
        await execute_attack_logic(interaction, self.target_code, self.active_world, self.total_cost)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(content="作戦を中止しました。", view=self)

class TroopModal(discord.ui.Modal, title="攻撃部隊の編成"):
    troops_input = discord.ui.TextInput(label="投入する資金 (消費額)", placeholder="例: 1000", required=True)
    def __init__(self, target_code: str, active_world: int):
        super().__init__()
        self.target_code = target_code; self.active_world = active_world

    async def on_submit(self, interaction: discord.Interaction):
        raw_val = self.troops_input.value.translate(str.maketrans('０１２３４５６７８９', '0123456789')).replace(',', '').strip()
        try: total_cost = int(raw_val)
        except ValueError: return await interaction.response.send_message("[エラー] 正しい数字を入力してください。", ephemeral=True)
        if total_cost <= 0: return await interaction.response.send_message("[エラー] 資金は1以上で入力してください。", ephemeral=True)

        guild_id, user_id, world_id = str(interaction.guild_id), str(interaction.user.id), self.active_world
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT confirm_attack FROM user_settings WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            row = c.fetchone(); confirm = row[0] if row else 1
            
            c.execute("SELECT owner_id FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, self.target_code))
            def_row = c.fetchone(); defender_id = def_row[0] if def_row else None
            
            base_cost_multiplier = 1.0
            if defender_id and not is_at_war(guild_id, world_id, user_id, defender_id):
                base_cost_multiplier = 1.5
            
            distance_penalty = 1.0
            c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
            row_adj = c.fetchone()
            if row_adj and row_adj[0] == 1:
                c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
                my_lands = [r[0] for r in c.fetchall()]
                if my_lands and not any(self.target_code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                    distance_penalty = 1.5

            final_cost_multiplier = base_cost_multiplier * distance_penalty
            actual_power = int(total_cost / final_cost_multiplier)

        if confirm == 1:
            warning = []
            if base_cost_multiplier > 1.0: warning.append("[警告] 未宣戦による奇襲ペナルティあり")
            if distance_penalty > 1.0: warning.append("[警告] 非隣接による遠征ペナルティあり")
            warn_str = "\n".join(warning) if warning else "[情報] ペナルティなし"

            view = AttackConfirmView(self.target_code, world_id, total_cost, actual_power, warn_str)
            msg = f"**最終確認**\n対象: **{self.target_code}**\n\n投入資金: **{total_cost} Gold**\n{warn_str}\n\n実質的な攻撃力: **{actual_power}**\nこの内容で出撃してよろしいですか？"
            await interaction.response.send_message(msg, view=view, ephemeral=True)
        else:
            await safe_defer(interaction)
            await execute_attack_logic(interaction, self.target_code, world_id, total_cost)

class AttackView(discord.ui.View):
    def __init__(self, author_id: int, target_code: str, active_world: int):
        super().__init__(timeout=60)
        self.author_id = author_id; self.target_code = target_code; self.active_world = active_world

    @discord.ui.button(label="進軍する", style=discord.ButtonStyle.danger)
    async def confirm_attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("[エラー] 操作できません。", ephemeral=True)
        await interaction.response.send_modal(TroopModal(self.target_code, self.active_world))
        for child in self.children: child.disabled = True
        try: await interaction.message.edit(view=self)
        except Exception: pass

    @discord.ui.button(label="取り消す", style=discord.ButtonStyle.secondary)
    async def cancel_attack(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id: return await interaction.response.send_message("[エラー] 操作できません。", ephemeral=True)
        for child in self.children: child.disabled = True
        try: await interaction.message.edit(content="作戦を中止しました。", embed=None, view=self)
        except Exception: pass

class AttackTargetModal(discord.ui.Modal, title="侵攻先の設定"):
    target_input = discord.ui.TextInput(label="攻撃目標の国コード", placeholder="例: JPN", required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await run_attack(interaction, self.target_input.value)

class DefendAmountModal(discord.ui.Modal, title="防衛費の投資"):
    amount_input = discord.ui.TextInput(label="1国あたりの投資額", placeholder="例: 100", required=True)
    def __init__(self, selected_codes, active_world):
        super().__init__()
        self.selected_codes = selected_codes; self.active_world = active_world
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try: amount = int(self.amount_input.value)
        except ValueError: return await interaction.followup.send("[エラー] 正しい数字を入力してください。", ephemeral=True)
        await execute_defend_logic(interaction, self.selected_codes, amount, self.active_world)

class DefendMultiSelect(discord.ui.Select):
    def __init__(self, territories, active_world):
        self.active_world = active_world
        options = [discord.SelectOption(label=t[0], description=f"現在の防衛力: {t[1]}") for t in territories[:25]]
        super().__init__(placeholder="投資する領土を選択 (複数可)", min_values=1, max_values=len(options), options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DefendAmountModal(self.values, self.active_world))

class DefendView(discord.ui.View):
    def __init__(self, territories, active_world):
        super().__init__(timeout=60)
        self.add_item(DefendMultiSelect(territories, active_world))

# ==============================================================================
# ロジック・ゲーム処理
# ==============================================================================
async def execute_attack_logic(interaction: discord.Interaction, target_code: str, active_world: int, total_cost: int):
    guild_id, user_id, user_name, world_id = str(interaction.guild_id), str(interaction.user.id), interaction.user.display_name, active_world
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        p_row = c.fetchone()
        current_gold, current_oil = (p_row[0], p_row[1]) if p_row else (0, 0)
        
        oil_enabled = is_oil_enabled(guild_id, world_id)
        oil_cost = total_cost if oil_enabled else 0
        if oil_enabled and current_oil < oil_cost: return await interaction.followup.send(f"[エラー] 石油が不足しています！(必要: {oil_cost} L / 現在: {current_oil} L)", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        def_row = c.fetchone()
        defender_id, base_def = (def_row[0], def_row[1]) if def_row else (None, DEFAULT_DEFENSE)

        if defender_id and is_allied(guild_id, world_id, user_id, defender_id): return await interaction.followup.send("[エラー] 同盟国の領土は攻撃できません！", ephemeral=True)
        if defender_id and is_peace_treaty_active(guild_id, world_id, user_id, defender_id): return await interaction.followup.send("[エラー] このプレイヤーとは不戦協定(Peace Treaty)を結んでいます！期間中は攻撃できません。", ephemeral=True)

        war_status_text, defense_power, base_cost_multiplier = "正規の戦争", base_def, 1.0
        if defender_id and not is_at_war(guild_id, world_id, user_id, defender_id):
            war_status_text, defense_power, base_cost_multiplier = "[警告] 奇襲/強行軍 (実質攻撃力が低下します)", int(base_def * 1.5), 1.5
        
        distance_penalty, distance_text = 1.0, ""
        c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
        row_adj = c.fetchone()
        if row_adj and row_adj[0] == 1:
            c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
            my_lands = [r[0] for r in c.fetchall()]
            if my_lands and not any(target_code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                distance_penalty = 1.5
                distance_text = "\n[警告] 遠征ペナルティ (実質攻撃力が低下します)"

        final_cost_multiplier = base_cost_multiplier * distance_penalty
        actual_power = int(total_cost / final_cost_multiplier)

        if current_gold < total_cost: return await interaction.followup.send(f"[エラー] 資金不足です。(現在: {current_gold} / 指定額: {total_cost} gold)", ephemeral=True)

        atk_roll, def_roll = int(actual_power * random.uniform(0.8, 1.2)), int(defense_power * random.uniform(0.8, 1.2))
        embed = discord.Embed(title=f"作戦報告: {target_code} 侵攻 [世界#{world_id}]")
        
        if atk_roll > def_roll:
            surviving_troops = max(10, actual_power - int(actual_power * random.uniform(0.1, 0.4)))
            npc_bonus = 500 if defender_id == "NPC_REBELS" else 0
            new_gold_val = max(0, current_gold - total_cost + npc_bonus)
            c.execute("UPDATE players SET gold=?, oil=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold_val, current_oil - oil_cost, guild_id, world_id, user_id))
            c.execute("INSERT OR REPLACE INTO territories (guild_id, world_id, iso_alpha, owner_id, defense) VALUES (?, ?, ?, ?, ?)", (guild_id, world_id, target_code, user_id, surviving_troops))
            embed.color = 0x2ecc71
            bonus_str = "\n🎉 **反乱軍を撃退！ 討伐ボーナスとして +500 Gold を獲得しました！**" if npc_bonus > 0 else ""
            embed.description = f"**作戦成功！ {user_name} が {target_code} を制圧しました！**\n\n作戦タイプ: {war_status_text}{distance_text}\n消費資金: **{total_cost}** Gold\n実質攻撃力: **{actual_power}** (ペナルティ適用後)\n残存配置兵力: {surviving_troops}人{bonus_str}"
            add_world_log(guild_id, world_id, f"{user_name} が {target_code} への侵攻作戦に成功し、制圧しました。")
            add_trophy(guild_id, world_id, user_id, "新進気鋭の征服者")
        else:
            new_defense = max(10, base_def - int(atk_roll * random.uniform(0.4, 0.8)))
            c.execute("UPDATE players SET gold=?, oil=? WHERE guild_id=? AND world_id=? AND user_id=?", (current_gold - total_cost, current_oil - oil_cost, guild_id, world_id, user_id))
            if defender_id: c.execute("UPDATE territories SET defense=? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (new_defense, guild_id, world_id, target_code))
            embed.color = 0xe74c3c
            embed.description = f"**作戦失敗... 防衛に阻まれ全滅しました。**\n\n作戦タイプ: {war_status_text}{distance_text}\n損失資金: **{total_cost}** Gold\n実質攻撃力: **{actual_power}**"
            add_world_log(guild_id, world_id, f"{user_name} の {target_code} への侵攻作戦は防衛軍に阻まれ失敗しました。")
        conn.commit()

    if defender_id:
        notify_users = set([defender_id])
        with get_db_connection() as conn3:
            c3 = conn3.cursor()
            c3.execute("SELECT user_a, user_b FROM alliances WHERE guild_id=? AND world_id=? AND (user_a=? OR user_b=?)", (guild_id, world_id, defender_id, defender_id))
            for ua, ub in c3.fetchall(): notify_users.add(ua); notify_users.add(ub)
        if user_id in notify_users: notify_users.remove(user_id)

        async def send_dms():
            guild = interaction.client.get_guild(int(guild_id))
            if not guild: return
            for uid in notify_users:
                member = guild.get_member(int(uid))
                if member and not member.bot:
                    try:
                        role_text = "あなたの領土" if uid == defender_id else f"同盟国 (<@{defender_id}>) の領土"
                        msg = f"[緊急事態発生 / 世界#{world_id}]\n{role_text} **【{target_code}】** が <@{user_id}> ({user_name}) から攻撃を受けました！至急状況を確認してください！"
                        await member.send(msg)
                    except Exception: pass
        interaction.client.loop.create_task(send_dms())

    map_file, _ = await asyncio.to_thread(_generate_current_map_sync, guild_id, world_id)
    content_msg = get_promo_and_tip()
    
    if map_file: await interaction.channel.send(content=content_msg, embed=embed.set_image(url="attachment://war_map.png"), file=map_file)
    else: await interaction.channel.send(content=content_msg, embed=embed)
    
    if interaction.response.is_done(): await interaction.followup.send("[完了] 作戦が完了しました。", ephemeral=True)
    else: await interaction.response.send_message("[完了] 作戦が完了しました。", ephemeral=True)

async def execute_defend_logic(interaction: discord.Interaction, target_codes: list[str], amount_per_country: int, world_id: int):
    if amount_per_country <= 0: return await interaction.followup.send("[エラー] 金額は1以上にしてください。", ephemeral=True)
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    total_cost = amount_per_country * len(target_codes)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        p_row = c.fetchone()
        if not p_row or p_row[0] < total_cost: return await interaction.followup.send(f"[エラー] 資金不足です。(必要: {total_cost} Gold)", ephemeral=True)
            
        c.execute("SELECT COUNT(*) FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        my_lands = c.fetchone()[0]
        is_empire = my_lands >= 3
        gained_defense = int(amount_per_country / 0.9) if is_empire else amount_per_country
        
        valid_targets = []
        for code in target_codes:
            c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, code))
            t_row = c.fetchone()
            if t_row and t_row[0] == user_id: valid_targets.append(code)
                
        if not valid_targets: return await interaction.followup.send("[エラー] 対象の領土が見つからないか、あなたの領土ではありません。", ephemeral=True)
            
        actual_total_cost = amount_per_country * len(valid_targets)
        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (actual_total_cost, guild_id, world_id, user_id))
        for code in valid_targets:
            c.execute("UPDATE territories SET defense=defense+? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (gained_defense, guild_id, world_id, code))
        conn.commit()
    
    empire_msg = "\n[大国ボーナス] 防衛コスト効率が上昇しました。" if is_empire else ""
    msg = f"[完了] {', '.join(valid_targets)} に各 {amount_per_country} Gold 投資しました。(増加防衛力: +{gained_defense}){empire_msg}"
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

async def run_status(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        check_and_create_user(c, guild_id, world_id, user_id, interaction.user.display_name)
        c.execute("SELECT gold, oil, main_country, debt FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        gold, oil, main_country, debt = row[0], row[1], row[2], row[3] if row else (0,0,"",0)
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        territories = c.fetchall()
    
    tax = sum(TERRITORY_YIELD.get(t[0], 50) for t in territories)
    oil_drain = len(territories) * 25
    oil_status = f"[システム有効]\n(次回: 配給2000 - 維持費{oil_drain} = **{2000 - oil_drain} L**)" if is_oil_enabled(guild_id, world_id) else "[無効]"

    embed = discord.Embed(title=f"国家ステータス [世界#{world_id}]", color=0xf1c40f)
    embed.add_field(name="メイン国家", value=f"**{main_country}**")
    embed.add_field(name="保有資金", value=f"**{gold}** Gold")
    embed.add_field(name="備蓄石油", value=f"**{oil}** L\n{oil_status}")
    embed.add_field(name="総兵力 (合計防衛力)", value=f"**{sum(t[1] for t in territories)}** 人")
    embed.add_field(name="次回の定時資金収入", value=f"基本 {BASE_INCOME} + 税収 {tax} = **{BASE_INCOME + tax}**")
    if debt and debt > 0: embed.add_field(name="借金 (戦債残高)", value=f"**{debt}** Gold\n(給付から自動天引き)")
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

async def run_diplomacy(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    view = discord.ui.View(timeout=60)
    view.add_item(DiplomacyActionSelect(world_id))
    await interaction.followup.send(f"**外務省パネル** [世界#{world_id}]\n実行したい外交アクションを選択してください。", view=view, ephemeral=True)

async def run_targets(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, owner_id FROM territories WHERE guild_id=? AND world_id=?", (guild_id, world_id))
        occupied = {row[0]: row[1] for row in c.fetchall()}
        c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        my_lands = [r[0] for r in c.fetchall()]
        
    adj_targets = set()
    for land in my_lands: adj_targets.update(ADJACENCY_GRAPH.get(land, set()))
    unoccupied_lands = [code for code in VALID_CODES if code not in occupied]
    enemy_lands = [code for code, owner in occupied.items() if owner != user_id and not is_allied(guild_id, world_id, user_id, owner)]
    
    def format_land(code): return f"`{code}`*" if code in adj_targets else f"`{code}`"
    
    embed = discord.Embed(title=f"侵攻可能な国家・地域一覧 [世界#{world_id}]", description="*マークは自国領土と隣接しています。", color=0xe74c3c)
    embed.add_field(name="無所属の空き地", value=" ".join([format_land(c) for c in sorted(unoccupied_lands)])[:1024] if unoccupied_lands else "なし", inline=False)
    embed.add_field(name="敵対国・他人の土地", value=" ".join([format_land(c) for c in sorted(enemy_lands)])[:1024] if enemy_lands else "なし", inline=False)
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, ephemeral=True)

async def run_country_management(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        territories = c.fetchall()
    if not territories: return await interaction.followup.send("[エラー] 管理できる領土を所有していません。", ephemeral=True)
    await interaction.followup.send(f"**領土管理 [世界#{world_id}]**\n下のメニューから管理したい領土を選択してください。", view=CountryManageMainView(territories, world_id, user_id), ephemeral=True)

async def run_country_status(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT iso_alpha, defense FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
        lands = c.fetchall()
    if not lands: return await interaction.followup.send(f"[世界#{world_id}] 領土はありません。", ephemeral=True)
    embed = discord.Embed(title=f"自国領土の防衛状況 [世界#{world_id}]", description="\n".join([f"・ **{iso}**: 防衛力 {defen}" for iso, defen in lands]), color=0x2ecc71)
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_attack(interaction: discord.Interaction, target: str, world_id_override: int = None):
    await safe_defer(interaction, ephemeral=True)
    if world_id_override is not None:
        world_id = world_id_override
    else:
        world_id = await ensure_world_context(interaction)
        if world_id == 0: return

    code = resolve_country_code(target)
    if code not in VALID_CODES: return await interaction.followup.send(f"[エラー] 存在しない国名です: {target}", ephemeral=True)
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, code))
        def_row = c.fetchone()
    
    defender_id = def_row[0] if def_row else None
    base_def = def_row[1] if def_row else DEFAULT_DEFENSE
    owner_name, warning_text = "無所属", ""
    base_cost_multiplier = 1.0

    if defender_id:
        if defender_id == user_id: return await interaction.followup.send("[エラー] すでに自国領土です。", ephemeral=True)
        try:
            owner_user = await interaction.client.fetch_user(int(defender_id))
            owner_name = owner_user.display_name
        except: pass
        if not is_at_war(guild_id, world_id, user_id, defender_id):
            base_cost_multiplier = 1.5
            warning_text = f"\n\n[警告] {code} は現在【**{owner_name}**】の領土です。\n宣戦布告なしで攻撃した場合、実質的な攻撃力が低下します。"

    distance_penalty = 1.0
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT adjacency_penalty FROM server_channels WHERE guild_id=?", (guild_id,))
        row_adj = c.fetchone()
        if row_adj and row_adj[0] == 1:
            c.execute("SELECT iso_alpha FROM territories WHERE guild_id=? AND world_id=? AND owner_id=?", (guild_id, world_id, user_id))
            my_lands = [r[0] for r in c.fetchall()]
            if my_lands and not any(code in ADJACENCY_GRAPH.get(l, set()) for l in my_lands):
                distance_penalty = 1.5

    final_cost_multiplier = base_cost_multiplier * distance_penalty
    min_est = max(100, int(base_def * final_cost_multiplier * 0.8))
    max_est = max(100, int(base_def * final_cost_multiplier * 1.2))

    embed = discord.Embed(title=f"作戦司令部 [世界#{world_id}]", description=f"対象: **{code}**{warning_text}\n準備ができたら進軍を押してください。", color=0x3498db)
    embed.add_field(name="作戦推定コスト", value=f"推定必要資金: **{min_est} 〜 {max_est} Gold**\n*(※敵の防衛力や乱数により変動します。投入資金はそのまま全額消費されます)*")
    
    await interaction.followup.send(content=get_promo_and_tip(), embed=embed, view=AttackView(interaction.user.id, code, world_id), ephemeral=True)

async def run_un_list(interaction: discord.Interaction):
    if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM un_members WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), world_id))
        members = c.fetchall()
    if not members: return await interaction.followup.send("加盟しているプレイヤーはいません。", ephemeral=True)
    embed = discord.Embed(title=f"国連(UN)加盟国一覧 [世界#{world_id}]", color=0x3498db)
    embed.description = "\n".join([f"・ <@{m[0]}>" for m in members])
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_camp_list(interaction: discord.Interaction):
    if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT camp_name, founder_id FROM camps WHERE guild_id=? AND world_id=?", (str(interaction.guild_id), world_id))
        camps = c.fetchall()
        if not camps: return await interaction.followup.send("現在、設立されている陣営はありません。", ephemeral=True)
        
        embed = discord.Embed(title=f"陣営一覧 [世界#{world_id}]", color=0x2ecc71)
        for camp_name, founder_id in camps:
            c.execute("SELECT user_id FROM camp_members WHERE guild_id=? AND world_id=? AND camp_name=?", (str(interaction.guild_id), world_id, camp_name))
            members = c.fetchall()
            member_count = len(members)
            embed.add_field(name=f"【{camp_name}】", value=f"設立者: <@{founder_id}>\n所属人数: {member_count}名", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

    # ==============================================================================
# アップデート情報読み込みロジック
# ==============================================================================
def get_versions():
    """updateフォルダ内のtxtファイルを取得し、新しい順にソートして返す"""
    update_dir = "update"
    if not os.path.exists(update_dir):
        return []
    
    versions = []
    for filename in os.listdir(update_dir):
        if filename.endswith(".txt"):
            versions.append(filename[:-4]) # .txtを消して追加
            
    # v1.0, v1.10 などのバージョンを正しく(1,0), (1,10)として比較して降順ソート
    def parse_version(v_str):
        clean_v = v_str.lower().replace('v', '')
        try:
            return tuple(map(int, clean_v.split('.')))
        except ValueError:
            return (0,)
            
    versions.sort(key=parse_version, reverse=True)
    return versions

async def run_version(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    versions = get_versions()
    if not versions:
        return await interaction.followup.send("[エラー] `update` フォルダ、またはバージョンファイルが見つかりません。", ephemeral=True)
        
    top_versions = versions[:10] # 最新から10件
    
    embed = discord.Embed(title="☁ 閲覧可能なバージョン一覧", color=0x3498db)
    description = ""
    for v in top_versions:
        description += f"・ **{v}**\n"
    
    description += "\n内容を見るには `/update [バージョン]` と入力してね！"
    embed.description = description
    
    await interaction.followup.send(embed=embed, ephemeral=True)

async def run_update(interaction: discord.Interaction, version: str = None):
    await safe_defer(interaction, ephemeral=True)
    versions = get_versions()
    if not versions:
        return await interaction.followup.send("[エラー] `update` フォルダ、またはバージョンファイルが見つかりません。", ephemeral=True)
        
    # 引数がなければ最新バージョンを使用
    target_version = version if version else versions[0]
    
    file_path = f"update/{target_version}.txt"
    
    # もし「1.0」と入力されたら「v1.0」として探す優しさフォールバック
    if not os.path.exists(file_path):
        if not target_version.startswith('v') and os.path.exists(f"update/v{target_version}.txt"):
            target_version = f"v{target_version}"
            file_path = f"update/{target_version}.txt"
        else:
            return await interaction.followup.send(f"[エラー] バージョン `{target_version}` の情報が見つかりません。", ephemeral=True)
            
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        embed = discord.Embed(title=f"☁ 全世界戦争Bot アップデート情報 ({target_version})", description=content, color=0x3498db)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"[エラー] ファイルの読み込みに失敗しました: {e}", ephemeral=True)

async def run_invest(interaction: discord.Interaction, amount: int):
    await safe_defer(interaction, ephemeral=True)
    if amount <= 0: return await interaction.followup.send("[エラー] 投資額は1 Gold以上にしてください。", ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, invest FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row: return await interaction.followup.send("[エラー] 国家データが見つかりません。", ephemeral=True)
        current_gold, current_invest = row[0], (row[1] if row[1] else 0)
        
        if current_gold < amount: return await interaction.followup.send(f"[エラー] 資金不足です。(現在: **{current_gold} Gold**)", ephemeral=True)
        
        new_gold = current_gold - amount
        new_invest = current_invest + amount
        c.execute("UPDATE players SET gold=?, invest=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold, new_invest, guild_id, world_id, user_id))
        conn.commit()
        
    await interaction.followup.send(f"📈 **[投資完了]**\n国家インフラに **{amount} Gold** を投資しました！\n現在の総投資額: **{new_invest}**" + get_promo_and_tip(), ephemeral=True)

async def run_research(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id = str(interaction.guild_id), str(interaction.user.id)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, oil, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row: return await interaction.followup.send("[エラー] 国家データが見つかりません。", ephemeral=True)
        
        gold, oil, tech_level = row[0], row[1], (row[2] if row[2] else 1)
        cost_gold = tech_level * 1000
        cost_oil = tech_level * 500
        
        if gold < cost_gold or oil < cost_oil:
            return await interaction.followup.send(f"🧪 **[研究失敗: 資源不足]**\n**Lv.{tech_level+1}** への研究には以下の資源が必要です。\n・資金: **{cost_gold} Gold** (現在: {gold})\n・石油: **{cost_oil} L** (現在: {oil})", ephemeral=True)
        
        c.execute("UPDATE players SET gold=gold-?, oil=oil-?, tech_level=tech_level+1 WHERE guild_id=? AND world_id=? AND user_id=?", (cost_gold, cost_oil, guild_id, world_id, user_id))
        conn.commit()
    await interaction.followup.send(f"🔬 **[研究完了]**\n技術研究が成功しました！国家のテックレベルが **Lv.{tech_level+1}** に上昇しました！" + get_promo_and_tip(), ephemeral=True)

async def run_spy(interaction: discord.Interaction, target: str):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id, target_code = str(interaction.guild_id), str(interaction.user.id), resolve_country_code(target)
    cost = 1500
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row or row[0] < cost: return await interaction.followup.send(f"[エラー] 資金 **{cost} Gold** が必要です。", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        t_row = c.fetchone()
        if not t_row: return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
        if t_row[0] == user_id: return await interaction.followup.send("[エラー] 自国にスパイは派遣できません。", ephemeral=True)

        tech_level = row[1] if row[1] else 1
        success_rate = min(95, 60 + (tech_level * 2))
        is_success = random.randint(1, 100) <= success_rate

        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

        if is_success:
            c.execute("SELECT user_name, gold, oil FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, t_row[0]))
            enemy = c.fetchone()
            msg = f"🕵️ **[潜入成功]**\n**{target_code}** への潜入に成功しました！\n\n**【極秘データ】**\n・統治者: **{enemy[0]}**\n・現在防衛力: **{t_row[1]}** 人\n・国家資金: **{enemy[1]}** Gold\n・石油備蓄: **{enemy[2]}** L\n・潜入成功率: {success_rate}%"
        else:
            msg = f"💥 **[潜入失敗]**\n防衛網に引っかかりスパイは捕らえられました...\n工作資金 **{cost} Gold** を失いました。(成功率: {success_rate}%)"
        conn.commit()
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)

async def run_propaganda(interaction: discord.Interaction, target: str):
    await safe_defer(interaction, ephemeral=True)
    world_id = await ensure_world_context(interaction)
    if world_id == 0: return
    guild_id, user_id, target_code = str(interaction.guild_id), str(interaction.user.id), resolve_country_code(target)
    cost = 3000
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT gold, tech_level FROM players WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
        row = c.fetchone()
        if not row or row[0] < cost: return await interaction.followup.send(f"[エラー] 資金 **{cost} Gold** が必要です。", ephemeral=True)

        c.execute("SELECT owner_id, defense FROM territories WHERE guild_id=? AND world_id=? AND iso_alpha=?", (guild_id, world_id, target_code))
        t_row = c.fetchone()
        if not t_row: return await interaction.followup.send(f"[エラー] {target_code} は中立国か、存在しない国です。", ephemeral=True)
        if t_row[0] == user_id: return await interaction.followup.send("[エラー] 自国には工作を行えません。", ephemeral=True)

        tech_level = row[1] if row[1] else 1
        success_rate = min(90, 40 + (tech_level * 3))
        is_success = random.randint(1, 100) <= success_rate

        c.execute("UPDATE players SET gold=gold-? WHERE guild_id=? AND world_id=? AND user_id=?", (cost, guild_id, world_id, user_id))

        if is_success:
            drop_amount = max(10, int(t_row[1] * random.uniform(0.10, 0.20)))
            new_def = max(100, t_row[1] - drop_amount)
            c.execute("UPDATE territories SET defense=? WHERE guild_id=? AND world_id=? AND iso_alpha=?", (new_def, guild_id, world_id, target_code))
            msg = f"📢 **[工作成功]**\n民衆を扇動し暴動を起こさせました！\n混乱により現地の防衛力が **{t_row[1] - new_def}** 減少しました！ (現在の防衛力: {new_def})"
        else:
            msg = f"❌ **[工作失敗]**\nプロパガンダは見破られ強制送還されました...\n工作資金 **{cost} Gold** を無駄にしました。"
        conn.commit()
    await interaction.followup.send(msg + get_promo_and_tip(), ephemeral=True)        
# ==============================================================================
# 不戦協定 (Peace Treaty)
# ==============================================================================
class PeaceTreatyView(discord.ui.View):
    def __init__(self, guild_id: str, world_id: int, proposer_id: str, proposer_name: str, target_id: str):
        super().__init__(timeout=86400)
        self.guild_id = guild_id
        self.world_id = world_id
        self.proposer_id = proposer_id
        self.proposer_name = proposer_name
        self.target_id = target_id

    @discord.ui.button(label="承認する", style=discord.ButtonStyle.success, emoji="🕊️")
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_id: return await interaction.response.send_message("あなたへの提案ではありません。", ephemeral=True)
        await interaction.response.defer()
        
        now = datetime.datetime.now(datetime.timezone.utc)
        expires = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO peace_treaties (guild_id, world_id, user_a, user_b, expires_at) VALUES (?, ?, ?, ?, ?)", (self.guild_id, self.world_id, self.proposer_id, self.target_id, expires))
            conn.commit()
            
        add_world_log(self.guild_id, self.world_id, f"{self.proposer_name} と {interaction.user.display_name} が24時間の不戦協定を締結しました。")
        self.stop()
        await interaction.message.edit(content=f"🕊️ **不戦協定締結**\n{self.proposer_name} と {interaction.user.display_name} は、向こう24時間の不戦協定を結びました！", view=None)

    @discord.ui.button(label="拒否する", style=discord.ButtonStyle.danger)
    async def btn_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_id: return await interaction.response.send_message("あなたへの提案ではありません。", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await interaction.message.edit(content=f"💥 **協定拒否**\n{interaction.user.display_name} は {self.proposer_name} の不戦協定提案を蹴り飛ばしました。", view=None)
