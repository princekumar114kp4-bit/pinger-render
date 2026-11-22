Keep-Alive Panel (Telegram se control)

Ek bot banao:
1. Telegram me @BotFather kholo, /newbot likho, naam aur username do.
2. Wo token copy karo jo milega. Ye secret hai, kisi ko mat dikhana.

Render par setup:
1. Render par New > Web Service banao (free plan chalega).
2. Build command: pip install -r requirements.txt
3. Start command: python pinger.py
4. Environment me ye variables daalo:
   - BOT_TOKEN = BotFather wala token
   - OWNER_ID = tumhara numeric Telegram ID (abhi nahi pata to ise khaali chhod do)
   - PING_URLS (optional) = URLs comma se alag karke, jaise https://a.com,https://b.com
   - PING_MINUTES (optional) = kitne minute mein ping kare, 1 se 60 tak (default 10)
5. Deploy karo.

Pehli baar:
- Apne bot me /start likho.
- Agar OWNER_ID nahi daala tha, to bot tumhara ID bhejega. Wo ID OWNER_ID me daalo, service restart karo, phir /start likho.
- Panel khul jayega. Buttons se URL add/remove, interval badlo, Pause/Resume karo, ya Ping now dabao.

Commands: /add https://...  /remove 2  /list  /interval 5  /pingnow  /pause  /resume  /status

Premium look:
- Panel abhi normal emoji se chalega. pinger.py me PREMIUM_EMOJI wali list khaali hai.
- Jinke Telegram par Premium nahi hai, unhe normal emoji hi dikhenge, koi error nahi aayega.
- Button ke rang (laal, hara, neela) Telegram app ke naye version par dikhte hain. Purane app me normal button dikhega.

Note:
- Ye bot sirf OWNER_ID wale user ke liye hai. Dusre log ko sirf "private hai" msg milega.
- Free plan par file reset ho sakti hai (redeploy ke baad). Tab list PING_URLS se wapas aayegi, ya /add se phir se daal do.
