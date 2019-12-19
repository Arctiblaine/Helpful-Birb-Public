import discord
from random import choice, randint
from discord.ext import commands
import random
from discord.utils import find
import sys
import datetime
import time
import json
import itertools
import copy
import asyncio
import os 
import traceback
from moves import all_moves
from legal_move import legal_move
from update_board import update_board
from async_timeout import timeout
from functools import partial
import functools
import math
from youtube_dl import YoutubeDL
from discord.ext import commands, tasks
from discord.ext.commands import Bot, has_permissions, MissingPermissions
from discord.utils import get
from itertools import cycle
from ctypes.util import find_library
from discord.voice_client import VoiceClient
from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL
import youtube_dl
from datetime import datetime
from discord import FFmpegPCMAudio

bot = commands.Bot(command_prefix='hb!')

cogs = ['cogs.music']

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 create_ytdl_player(url=self.url, ytdl_options=self.ytdl_format_options, before_options=beforeArgs)",
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError, commands.Cog):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError, commands.Cog):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer, commands.Cog):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        await ctx.send(f'```ini\nAdded {data["title"]} to the Queue.\n```')

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=True)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer(commands.Cog):
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**Now Playing:** `{source.title}` requested by '
                                               f'`{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            # this bit caused the entire process to close. we don't want that.
            '''source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass'''

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel=None):
        """Connect to voice.
        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.
        This command also handles moving the bot to different channels.
        """
        if not discord.opus.is_loaded():
            discord.opus.load_opus('libopus.so')
                       
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                raise InvalidVoiceChannel('No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        await ctx.send(f'Connected to: **{channel}**')

    @commands.command(name='play', aliases=['p'])
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue.
        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.
        Parameters
        ------------
        search: str [Required]
            The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.send("I wasn't in a channel, but now I am.")
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=True)

        await player.queue.put(source)

    @commands.command(name='pause')
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('I am not playing anything.')
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**{ctx.author}** paused the song.')

    @commands.command(name='resume')
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not playing anything.')
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**{ctx.author}**: resumed the song!')

    @commands.command(name='skip')
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not playing anything.' )

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'**{ctx.author}** skipped the song!')

    @commands.command(name='queue', aliases=['q', 'playlist'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to a channel.' )

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('The queue is empty.')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='now_playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not connected to voice.' )

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('I am not playing anything.')

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f'**Now Playing:** {vc.source.title} '
                                   f'requested by {vc.source.requester}')

    @commands.command(name='volume', aliases=['vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I am not currently connected to voice!' )

        if not 0 < vol < 101:
            return await ctx.send('Please enter a value between 1 and 100.')

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        await ctx.send(f'**`{ctx.author}`** set the volume to **{vol}%**')

    @commands.command(name='stop', aliases=['leave'])
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('I\'m not playing anything right now.' )

        await self.cleanup(ctx.guild)                       
                        
def_board = "011111111111111"
board_str = "011111111111111"
solvable = ["000000000000001",
            "000000000000010",
            "000000000000100",
            "000000000001000",
            "000000000010000",
            "000000000100000",
            "000000001000000",
            "000000010000000",
            "000000100000000",
            "000001000000000",
            "000010000000000",
            "000100000000000",
            "001000000000000",
            "010000000000000",
            "100000000000000"]
                        
def commands_birb():
	class Slapper(commands.Converter):
		async def convert(self, ctx, argument):
			to_slap = choice(ctx.guild.members)
			return '{0.author} slapped {1} because *{2}*'.format(ctx, to_slap, argument)

	@bot.command()
	async def blame(ctx, *, reason: Slapper):
		await ctx.send(reason)

	@bot.command()
	async def slap(ctx, user):
		await ctx.send('{0.author} slapped {1}'.format(ctx, user))


bot.add_cog(Music(bot))
commands_birb()                          
                        
performed_moves = []
all_legal_moves = all_moves(5)

client = bot
# initialises audio player
async def audio_player_task():
    while True:
        play_next_song.clear()
        current = await songs.get()
        await play_next_song.wait()

# defines toggle next song function
def toggle_next():
    client.loop.call_soon_threadsafe(play_next_song.set)

def text_getter(filename):
    '''
        Opens a file and returns a list of quotes.
    '''
    quest = []
    file_content = open(filename, 'r')
    for lines in file_content:
        if lines[0] == '#':
            continue
        else:
            quest.append(lines)
    index = random.randint(0, len(quest) - 1)
    return quest[index]

@bot.command()
async def disconnect(ctx):
    '''
        Bruteforce stop the music.
    '''
    try:
        channel = ctx.author.voice.channel
    except:
        return await ctx.send("You must be in a voice channel first.")
    async with ctx.typing():
        ctx.voice_client.stop()
    await ctx.send("I stopped the music.")

@bot.command()
async def joke(ctx):
    jokes = {"What does James Bond do before bed?":"He goes undercover.",
             "What is Gen Z's least favorite toy?":"A BOOMERang.",
             "Why were people running towards Finland?":"It was a race to the Finnish.",
             "The first guy that figured out how to split an atom,":"must’ve been blown away",
             "Did you hear about the guy who invented the knock-knock joke?":"He won the **NO BELL** prize.",
             "I used to hate facial hair...":"...but then it grew on me!",
             "Yes! I want to sell my vacuum cleaner...":"...because it was just gathering dust!",
             "Why can't you hear a psychiatrist using the bathroom?":"Because the 'P' is silent!",
             "Can February March?":"No, but April May!",
             "Do you want to hear a joke about paper?":"That's fine, it's TEARable.",
             "Puns make me numb":"Math puns make me number.",
             "My book on clocks finally arrived":"It's about time!",
             "Which weighs more, a gallon of water or a gallon of butane?":"A gallon of water. Butane is LIGHTER fluid.",
             "What did the 2 say to the 1 when he got injured?":"Do you need First Aid?",
             "Why does Waldo wear stripes?":"Because he doesn't want to be spotted.",
             "What do you call a dinner at a fancy restaurant with your 3 year old?":"Whine and dine.",
             "Why do pirates love Reddit?":"'Tis the best place to trade stolen content for gold.",
             "See that post above?":"That's the post above.",
             "You know...":"I once visited a crematorium that gave discounts for burn victims.",
             "A neutron walks into a bar. 'How much for a drink here, anyway?'":"To which the bartender responds, 'For you, no charge.'",
             "Photons have mass?":"I didn't even know they were Catholic.",
             "Did you know?":"It's common knowledge that irradiated cats have 18 half-lives.",
             "I was going to attend the clairvoyants meeting...":"...but it was cancelled due to unforseen events.",
             "Two atoms are in a bar. One says 'I think I lost an electron.'":"The other says 'Are you sure?' to which the other replies, 'I'm positive.'",
             "Two cannibals are eating a clown.":"One cannibal turns to the other and asks, 'Does this taste funny to you?'",
             "War does not determine who is right.":"Only who is left.",
             "Ah, did I tell you?":"The best contraceptive for old people is nudity.",
             "What kind of a doctor is Dr. Pepper?":"A 'fizz'-ician.", "Ash used to be wood...":"...but it was fired."}
    joke_key = random.choice(list(jokes.keys()))
    await ctx.send(joke_key)
    await asyncio.sleep(5)
    await ctx.send(jokes[joke_key])

@bot.command()
async def eightball(ctx, *, question):
    responses = ['It is certain', 'It is decidedly so', 
                'Without a doubt', 'Yes - definitely', 
                'You may rely on it', 'As I see it, yes', 
                'Most likely', 'Outlook good', 'Signs point to yes', 
                'Yes', 'Reply hazy, try again', 'Ask again later', 
                'Better not tell you now', 'Cannot predict now', 
                'Concentrate and ask again', 'Dont count on it', 
                'My reply is no', 'My sources say no', 
                'Outlook not so good', 'Very doubtful',
                 '...what?']
    await ctx.send(f'Question: {question}\nAnswer: {random.choice(responses)}')

@bot.command()
async def board(ctx, arg='', mov1='', mov2='', mov3=''):
    global board_str
    global performed_moves
    global solvable
    global def_board
    global board_str
    global performed_moves
    if arg=='':
        await ctx.send("This is the current state of the board.")
        await ctx.send("```    " + board_str[0] + "\n   " + " ".join(board_str[1:3]) + "\n  " + " ".join(board_str[3:6]) + "\n " + ' '.join(board_str[6:10]) + "\n" + ' '.join(board_str[10:16]) + '```')

    if arg=='help':
        await ctx.send("By default, I give out a board (hb!board). Though you see it as zeros and ones, I see it as:")
        await asyncio.sleep(5)
        board_str = "012345678901234"
        board_rep = "```    " + board_str[0] + "    \n   " + " ".join(board_str[1:3]) + "   \n  " + " ".join(board_str[3:6]) + "  \n " + ' '.join(board_str[6:10]) + "\n" + ' '.join(board_str[10:16]) + '```'
        await ctx.send(board_rep)
        await ctx.send("*That reocurring 0 after 9 means 10, and everything past it is 10 + 1.*")
        await asyncio.sleep(15)
        await ctx.send("Legal moves would be a 0 1 1 or 1 1 0. Your goal is to get the board to exactly one pin. So, for example:")
        await asyncio.sleep(15)
        await ctx.send("If peg 0, 1, 3 represented 0, 1, 1 then it's a legal move and I update the board to reflect it, leaving 0, 1, 3 as 1, 0, 0. If you ever get stuck, you can call hb!board legal to get a list of all *possible* legal moves. If you want to see which moves were used, call hb!board used.")
        await asyncio.sleep(30)
        await ctx.send("If what I said doesn't make sense, watch this video instead: https://www.youtube.com/watch?v=kZ6zr_EG5eI&t=0s")

    if arg=='used':
        await ctx.send("Here's a list of used moves:")
        if len(performed_moves) == 0:
            await ctx.send("No one has performed any moves on the current board.")
        else:
            await ctx.send(performed_moves)

    if arg=='solve':
        await ctx.send("Time to see how well you did with the board...")
        count = 0
        if board_str in solvable:
            await ctx.send("You solved the board! Try doing solving the board another way now.")
        else:
            await ctx.send("You didn't solve the board, but I'll see how well you did. Give me a moment...")
            await asyncio.sleep(5)
            for i in board_str:
                if i == 1:
                    count += 1

            if count == 2:
                await ctx.send("Not bad, you almost had it! I'm sure you'll get it again")
            elif count == 3:
                await ctx.send("With a little more practice, and patience, you can do it.")
            elif count == 4:
                await ctx.send("Try challenging the board again.")
            else:
                await ctx.send("...please tell me you ran this command by mistake?")

        await ctx.send("You can always reset the board with hb!board clear")

    if arg=='clear':
        await ctx.send('Resetting the board...')
        board_str = def_board
        performed_moves = []
        await ctx.send("The board has been reset. I reset the used moves, as well.\n")
        await ctx.send("```    " + board_str[0] + "\n   " + " ".join(board_str[1:3]) + "\n  " + " ".join(board_str[3:6]) + "\n " + ' '.join(board_str[6:10]) + "\n" + ' '.join(board_str[10:16]) + '```')
        
        return board_str and performed_moves
        
    if arg=='move':
        try:
            moveset = (int(mov1), int(mov2), int(mov3))
        except:
            await ctx.send("Send your moves like hb!board move 0 1 3 OR you didn't send integers.")

        if moveset in performed_moves:
            await ctx.send("Someone already performed this move. You can see which moves have been used with hb!board used")
            return

        yes = legal_move(board_str, moveset)
        if yes is True:
            await ctx.send("Your move is legal.")
            performed_moves.append(moveset)
            board_str = update_board(board_str, moveset)
            await ctx.send("The board has been updated.")
            await ctx.send("```    " + board_str[0] + "    \n   " + " ".join(board_str[1:3]) + "   \n  " + " ".join(board_str[3:6]) + "  \n " + ' '.join(board_str[6:10]) + "\n" + ' '.join(board_str[10:16]) + '```')
        else:
            await ctx.send("Your move is not legal. Try another move or look at a list of possible moves with hb!board legal")

        return board_str
        
    if arg=='legal':
        has_not_been_used = []
        await ctx.send("Here's a list of all (possible) moves: ")
        for i in all_legal_moves:
            if i in performed_moves:
                continue
            else:
                has_not_been_used.append(i)

        await ctx.send(has_not_been_used)

    else:
        await ctx.send("This is the current state of the board.")
        await ctx.send("```    " + board_str[0] + "\n   " + " ".join(board_str[1:3]) + "\n  " + " ".join(board_str[3:6]) + "\n " + ' '.join(board_str[6:10]) + "\n" + ' '.join(board_str[10:16]) + '```')

@bot.command()
async def whoami(ctx):
    user = ctx.message.author
    await ctx.send("Give me a moment...")
    await ctx.send("-flips through a book-")
    await asyncio.sleep(2)
    await ctx.send(
        "Here is what I know about you <@{0.id}>:\n"
        "Display name: {0.display_name}\n"
        "Username: {0.name}\n"
        "Discriminator: {0.discriminator}\n"
        "ID: {0.id}\n".format(user))

@bot.command()
async def time(ctx):
    currentDT = datetime.datetime.now()
    await ctx.send('My current time is: ')
    await ctx.send(currentDT.strftime("%I:%M:%S %p"))
    await ctx.send(currentDT.strftime("%a, %b %d, %Y"))

@bot.command()
async def quote(ctx):
    quote = text_getter("quote-file.txt")
    await ctx.send(quote)

@bot.command()
async def despacito(ctx):
    choice = random.randint(1, 2)
    if choice == 1:
        await ctx.send('https://www.youtube.com/watch?v=kJQP7kiw5Fk')
    elif choice == 2:
        await ctx.send('https://www.youtube.com/watch?v=W3GrSMYbkBE')

# Clear command, only for users who can manage messages
@client.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount : int):
    await ctx.channel.purge(limit = amount)

# outputs an error if the clear command fails
@clear.error
async def clear_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('Please specify an amount of messages to delete (including the command message). ')
    else:
        await ctx.send("You don't have permissions for that.")

@bot.command()
async def insult(ctx):
    quote = text_getter("insult.txt")
    await ctx.send(quote)

@bot.command()
async def fact(ctx):
    quote = text_getter("fact.txt")
    await ctx.send(quote)

@bot.command()
async def interro(ctx):
    quote = text_getter("interrogation-questions.txt")
    await ctx.send(quote)

@bot.command()
async def DanseisSynthDaddy(ctx):
    await ctx.send('https://i.pinimg.com/originals/78/ed/90/78ed90de5a3f18f5fa286169781b6d56.png')

@bot.command()
async def calc(ctx, oper, a, b):
    # add more support for calc.
    try:
        a = int(a)
        b = int(b)
        if oper == 'add' or oper == '+':
            await ctx.send(a+b)
        elif oper == 'multiply' or oper == '*':
            await ctx.send(a*b)
        elif oper == 'div' or oper == '/':
            if b == 0:
                await ctx.send("Did you not learn anything in basic math? You can't divide a number over zero you unsalted avocado.")
            else:
                await ctx.send(float(a/b))
        elif oper == 'sub' or oper == '-':
            await ctx.send(a - b)
        else:
            await ctx.send('Looks like I couldn\'t use that. Try:')
            await ctx.send('[add arg1 arg2], [multiply arg1 arg2], [div arg1 arg2],')
            await ctx.send('[sub arg1 arg2]')
    except:
        await ctx.send("I think you're missing a few arguments.")

@bot.command()
# idea by IronicallyIronic
async def number(ctx, inp1, inp2):
    inp1 = int(inp1)
    inp2 = int(inp2)
    if inp1 > inp2:
        rand = random.randint(inp2, inp1)
        await ctx.send('Your random number is: ')
        await ctx.send(rand)
    elif inp1 < -1 or inp2 < -1:
        await ctx.send('Please send your inputs as positive integers.')
    elif inp1 == inp2:
        await ctx.send(inp1)
    else:
        rand = random.randint(inp1, inp2)
        await ctx.send('Your random number is: ')
        await ctx.send(rand)

@bot.command()
# modified idea by Toasty
async def cat(ctx):
    random_inp = str(random.randint(1, 10))
    if random_inp == '1':
        await ctx.send("http://giphygifs.s3.amazonaws.com/media/6C4y1oxC6182MsyjvK/giphy.gif")
    elif random_inp == '2':
        await ctx.send("https://media.giphy.com/media/WYEWpk4lRPDq0/giphy.gif")
    elif random_inp == '3':
        await ctx.send("http://giphygifs.s3.amazonaws.com/media/S6VGjvmFRu5Qk/giphy.gif")
    elif random_inp == '4':
        await ctx.send("http://giphygifs.s3.amazonaws.com/media/FZuRP6WaW5qg/giphy.gif")
    elif random_inp == '5':
        await ctx.send("https://media.giphy.com/media/rwCX06Y5XpbLG/giphy.gif")
    elif random_inp == '6':
        await ctx.send("https://media.giphy.com/media/10SAlsUFbyl5Dy/giphy.gif")
    elif random_inp == '7':
        await ctx.send("https://media.giphy.com/media/tBxyh2hbwMiqc/giphy.gif")
    elif random_inp == '8':
        await ctx.send("http://giphygifs.s3.amazonaws.com/media/iTOS89Y0gD1ny/giphy.gif")
    elif random_inp == '9':
        await ctx.send("http://giphygifs.s3.amazonaws.com/media/2QHLYZFJgjsFq/giphy.gif")
    elif random_inp == '10':
        await ctx.send("https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif")
    else:
        await ctx.send("https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif")

@bot.command()
async def feature_request(ctx):
    await ctx.send("If you want to give Arctiblaine#8015 some feeback and/or a feature request, visit this Google doc link: https://docs.google.com/document/d/1KIWPKqFeCvEw7NA4-NTHNvG4r8xrvHwqtRxhT1D25GA/edit")

@bot.command()
async def info(ctx):
    embed = discord.Embed(title="Helpful Birb", description="A very helpful birb.", color=0xeee657)

    # give info about you here
    embed.add_field(name="Elder Developer:", value="Arctiblaine#8015", inline = False)

    # Shows the number of servers the bot is member of.
    embed.add_field(name="Servers I'm on:", value=f"{len(bot.guilds)}", inline = False)

    # give users a link to invite thsi bot to their server
    embed.add_field(name="Invite to Discord.py Server:", value="https://discord.gg/dpy", inline = False)

    await ctx.send(embed=embed)

bot.remove_command('help')

@bot.command()
async def help(ctx, arg=''):
    if arg == '':
        embed = discord.Embed(title = "All Commands", description = "A list of all my current commands.", color=0x45F4E9)
        embed.add_field(name = "Music", value = "Do hb!help music for more info about these commands.", inline = False)
        embed.add_field(name = "Board / Peg Solitaire", value = "Do hb!help board for more info about these commands.", inline = False)
        embed.add_field(name = "Misc", value = "Do hb!help misc for more info about these commands", inline = False)
        embed.add_field(name = "Fun", value = "Do hb!help fun for more info about these commands", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)
        
    elif arg == 'music' or arg == 'Music':
        embed = discord.Embed(title = "Music", description = "A list of all my music player commands.", color=0x45F4E9)
        embed.add_field(name = "hb!join", value = "Join the current channel you are in.", inline = False)
        embed.add_field(name = "hb!play <link>", value = "Request a song and add it to the queue, play the song if the queue is empty.", inline = False)
        embed.add_field(name = "hb!pause", value = "Pause the currently playing song.", inline = False)
        embed.add_field(name = "hb!resume", value = "Resume the currently paused song.", inline = False)
        embed.add_field(name = "hb!skip", value = "Skip the current song.", inline = False)
        embed.add_field(name = "hb!queue", value = "Retrieve a queue of upcoming songs.", inline = False)
        embed.add_field(name = "hb!playing", value = "Display information about the currently playing song.", inline = False)
        embed.add_field(name = "hb!vol <number OR float>", value = "Change the music player volume.", inline = False)
        embed.add_field(name = "hb!stop", value = "Stop the currently playing song and leave the voice channel.", inline = False)
        embed.add_field(name = "hb!disconnect", value = "A bruteforce disconnect if Helpful Birb is stuck in the voice channel.", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)
        
    elif arg == 'board' or arg == 'Board':
        embed = discord.Embed(title = "Board / Peg Solitaire", description = "A list of all these current commands.", color=0x45F4E9)
        embed.add_field(name = "hb!board", value = "Gives you the current state of the peg board.", inline = False)
        embed.add_field(name = "hb!board solve", value = "Determines if you solved the board.", inline = False)
        embed.add_field(name = "hb!board clear", value = "Resets the entire board.", inline = False)
        embed.add_field(name = "hb!board move position1 position2 position3", value = "Attempts a move on the board.", inline = False)
        embed.add_field(name = "hb!board legal", value = "Returns a list of all *possible* moves.", inline = False)
        embed.add_field(name = "hb!board used", value = "Returns a list of all *used* moves.", inline = False)
        embed.add_field(name = "hb!board help", value = "Not entirely sure what these do? Use this command.", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)
        
    elif arg == 'misc' or arg == 'Misc':
        embed = discord.Embed(title = "Miscellaneous.", description = "A list of all my other commands.", color=0x45F4E9)
        embed.add_field(name = "hb!whoami", value = "Who are you?", inline = False)
        embed.add_field(name = "hb!calc <operation> int1 int2", value = "Performs some basic math depending on the called operation.", inline = False)
        embed.add_field(name = "hb!clear <int>", value = "*Admins only* Cleans up the channel by the specified integer.", inline = False)
        embed.add_field(name = "hb!number int1 int2", value = "Picks one random number between integer 1 and integer 2.", inline = False)
        embed.add_field(name = "hb!feature_request", value = "Send some information about adding more commands to Helpful Birb.", inline = False)
        embed.add_field(name = "hb!info", value = "Sends you information on who made me and more.", inline = False)
        embed.add_field(name = "hb!help", value = "Sends this command.", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)
        
    elif arg == 'fun' or arg == 'Fun':
        embed = discord.Embed(title = "Fun", description = "A list of all my fun commands.", color=0x45F4E9)
        embed.add_field(name = "hb!joke", value = "Sends you a joke.", inline = False)
        embed.add_field(name = "hb!eightball <question>", value = "Gives insight on a given question.", inline = False)
        embed.add_field(name = "hb!quote", value = "Sends a random, out of context, quote.", inline = False)
        embed.add_field(name = "hb!despacito", value = "This is so sad. Alexa, play Despacito.", inline = False)
        embed.add_field(name = "hb!insult", value = "Sends an insult.", inline = False)
        embed.add_field(name = "hb!slap <user>", value = "Slap a user!", inline = False)
        embed.add_field(name = "hb!blame <reason", value = "You didn't slap that user. Blame a random user.", inline = False)
        embed.add_field(name = "hb!fact", value = "Sends a random fact.", inline = False)
        embed.add_field(name = "hb!interro", value = "Sends you a *'truth or dare question,'* an interrogation question.", inline = False)
        embed.add_field(name = "hb!DanseisSynthDaddy", value = "Sends a picture of the synth daddy himself.", inline = False)
        embed.add_field(name = "hb!cat", value = "Sends a random cat picture.", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)
        
    else:
        embed = discord.Embed(title = "All Commands", description = "A list of all my current commands.", color=0x45F4E9)
        embed.add_field(name = "Music", value = "Do hb!help music for more info about these commands.", inline = False)
        embed.add_field(name = "Board / Peg Solitaire", value = "Do hb!help board for more info about these commands.", inline = False)
        embed.add_field(name = "Misc", value = "Do hb!help misc for more info about these commands", inline = False)
        embed.add_field(name = "Fun", value = "Do hb!help fun for more info about these commands", inline = False)
        embed.set_footer(text = "A very helpful birb.")
        await ctx.send(embed=embed)

@client.event
async def on_command_error(ctx, error):
    await ctx.send(f"Error: **{error}**")

@bot.event
async def on_ready():
    print('Logged in as')
    print(bot.user.name)
    print(bot.user.id)
    print('------')
    await bot.change_presence(activity=discord.Game(name='hb!help'))

token = ""
bot.run(token)
