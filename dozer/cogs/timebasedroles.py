"""Adds time based roles to the bot"""
import datetime
from loguru import logger
import re

import discord
from discord.ext import tasks
from discord.ext.commands import cooldown, BucketType, guild_only, BadArgument, MissingPermissions, has_permissions

from dozer.context import DozerContext
from ._utils import *
from .. import db


class TimeBasedRoles(Cog):
    """Time based roles."""

    def __init__(self, bot):
        self.bot = bot

    @Cog.listener('on_member_join')
    async def on_member_join(self, member: discord.Member):
        guild_roles = await TimeBasedRole.get_by(guild_id=member.guild.id)
        if len(guild_roles) == 0: return

        join_timestamp = int(member.joined_at.timestamp())
        existing = await EarliestMemberJoin.get_by(guild_id=ctx.guild.id, user_id=user_id)
        if not existing or existing[0].timestamp >= join_timestamp:
            await EarliestMemberJoin(guild_id=member.guild.id,
                                     user_id=member._id,
                                     source_channel_id=None,
                                     timestamp=join_timestamp).update_or_add()

    @command()
    @guild_only()
    @has_permissions(manage_roles=True)
    async def importjoinlog(self, ctx: DozerContext, *, channel: discord.TextChannel):
        """Imports a channel's history for the purposes of time based roles.
        Scans each message for a user id.
        """

        await ctx.send("Importing.")
        async for message in channel.history(limit=None):
            # discord snowflakes are at least 15 digits long
            ids = re.findall(r'\d{15,}', message.content)
            if len(ids) == 0: continue

            # assumes the first snowflake in the message is a user id
            user_id = int(ids[0])

            existing = await EarliestMemberJoin.get_by(guild_id=ctx.guild.id, user_id=user_id)

            message_timestamp = int(message.created_at.timestamp())
            if not existing or existing[0].timestamp >= message_timestamp:
                await EarliestMemberJoin(guild_id=ctx.guild.id,
                                         user_id=user_id,
                                         source_channel_id=channel.id,
                                         timestamp=message_timestamp).update_or_add()

        await ctx.send("Imported.")

    @group(invoke_without_command=True)
    @guild_only()
    @has_permissions(manage_roles=True)
    async def timebasedrole(self, ctx:DozerContext):
        """Manages time based roles for this guild.
        Commands: add, remove, list.
        """
        await ctx.send("Manages time based roles for this guild.\nCommands: add, remove, list, refresh.")

    @timebasedrole.command()
    @guild_only()
    @has_permissions(manage_roles=True)
    async def add(self, ctx:DozerContext, *, role: discord.Role, time_required: int):
        """Adds a time based role for this guild. time_required is in seconds.
        """
        role = TimeBasedRole(guild_id=ctx.guild.id,
                             role_id=role.id,
                             time_required=time_required)
        await role.update_or_add()
        await ctx.send("Time based role added.")

    @timebasedrole.command()
    @guild_only()
    @has_permissions(manage_roles=True)
    async def remove(self, ctx:DozerContext, *, role: discord.Role):
        """Removes a time based role for this guild. Does not delete the role.
        """
        await TimeBasedRole.delete(guild_id=ctx.guild.id, role_id=role.id)
        await ctx.send("Time based role removed.")

    @timebasedrole.command()
    @guild_only()
    @has_permissions(manage_roles=True)
    async def list(self, ctx: DozerContext):
        """Lists time based roles for this guild.
        """
        roles = await TimeBasedRole.get_by(guild_id=ctx.guild.id)
        e = discord.Embed()

        if len(roles) > 0:
            for role in roles:
                e.add_field(name=f"{role.time_required}s", value=f"<@&{role.role_id}>")
        else:
            e.add_field(name='Error', value='No time based roles on this guild')

        await ctx.send(embed=e)

    @tasks.loop(hours=24)
    async def refresh(self):
        start_timestamp = int(datetime.datetime.now().timestamp())
        logger.debug("Starting time based role refresh.")

        guild_ids = set(role.guild_id for role in (await TimeBasedRole.get_by()))

        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild == None: continue

            roles = await TimeBasedRole.get_by(guild_id=guild_id)
            roles.sort(key=lambda r: r.time_required, reverse=True)

            for member in guild.members:
                last_join = member.joined_at.timestamp()
                earliest_joins = await EarliestMemberJoin.get_by(guild_id=guild_id, user_id=member.id)

                earliest_join = None
                if len(earliest_joins) == 0 or last_join < earliest_joins[0].timestamp:
                    earliest_join = EarliestMemberJoin(guild_id=guild_id,
                                                       user_id=member.id,
                                                       source_channel_id=None,
                                                       timestamp=last_join)
                    await earliest_join.update_or_add()
                else:
                    earliest_join = earliest_joins[0]

                time_on_guild = start_timestamp - earliest_join.timestamp

                for role in roles:
                    if role.time_required <= time_on_guild:
                        logger.debug(f"Giving f{member.id}: f{role.role_id}")
                        await member.add_roles(guild.get_role(role.role_id))

                        roles_clone = list(roles)
                        roles_clone.remove(role)
                        await member.remove_roles(
                            *[guild.get_role(r.role_id) for r in roles_clone]
                        )

                        break




class TimeBasedRole(db.DatabaseTable):
    """Holds info on member roles used for timeouts"""
    __tablename__ = 'time_based_roles'
    __uniques__ = 'guild_id, role_id'

    @classmethod
    async def initial_create(cls):
        """Create the table in the database"""
        async with db.Pool.acquire() as conn:
            await conn.execute(f"""
            CREATE TABLE {cls.__tablename__} (
            guild_id bigint not null,
            role_id bigint not null,
            time_required bigint not null,
            PRIMARY KEY (guild_id, role_id)
            )""")

    def __init__(self, guild_id: int, role_id: int, time_required: int):
        super().__init__()
        self.guild_id = guild_id
        self.role_id = role_id
        # time required is stored in seconds
        self.time_required = time_required

    @classmethod
    async def get_by(cls, **kwargs):
        results = await super().get_by(**kwargs)
        result_list = []
        for result in results:
            obj = TimeBasedRole(guild_id=result.get("guild_id"),
                                role_id=result.get("role_id"),
                                time_required=result.get("time_required"))
            result_list.append(obj)
        return result_list

class EarliestMemberJoin(db.DatabaseTable):
    """Holds info on the earliest known guild join for a given member."""
    __tablename__ = 'earliest_member_joins'
    __uniques__ = 'guild_id, user_id'

    @classmethod
    async def initial_create(cls):
        """Create the table in the database"""
        async with db.Pool.acquire() as conn:
            await conn.execute(f"""
            CREATE TABLE {cls.__tablename__} (
            guild_id bigint not null,
            user_id bigint not null,
            source_channel_id bigint,
            timestamp bigint not null,
            PRIMARY KEY (guild_id, user_id)
            )""")

    def __init__(self, guild_id: int, user_id: int, source_channel_id: int, timestamp: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.source_channel_id = source_channel_id
        self.timestamp = timestamp

    @classmethod
    async def get_by(cls, **kwargs):
        results = await super().get_by(**kwargs)
        result_list = []
        for result in results:
            obj = EarliestMemberJoin(guild_id=result.get("guild_id"),
                                     user_id=result.get("user_id"),
                                     source_channel_id=result.get("source_channel_id"),
                                     timestamp=result.get("timestamp"))
            result_list.append(obj)
        return result_list

async def setup(bot):
    """Adds the TimesBasedRoles cog to the bot."""
    await bot.add_cog(TimeBasedRoles(bot))
