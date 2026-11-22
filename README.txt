Keep-Alive Panel (Telegram se control)

Panel ke buttons chat box ke upar dikhte hain (keyboard), message ke neeche nahi.
Button dabane par uska naam apne aap chat me message ban kar bot ko chala jata hai.
Button dabane ke baad aapka dabaya hua message bhi hat jata hai, aur purana panel bhi. Naya panel neeche aata hai.

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
- Add URL dabao to bot URL maangega (2 minute tak). Cancel dabao to ruk jayega.
- Har action ke baad panel ek naya message bankar aata hai, purana panel aur aapka tap kiya hua message hat jata hai.
- Close dabane par buttons hat jaate hain. Phir se panel ke liye /start likho.

Bot ka access (lock / open):
- Bot naye setup par LOCKED hota hai. Matlab sirf owner aur jinhe owner ne allow kiya ho, wo panel use kar sakte hain.
- Baaki log /start ya koi bhi message bhejein, to unhe ek line milti hai: "Ye bot abhi private hai." Saath me ek button hota hai "Request access".
- "Request access" dabane par unki request owner ko jaati hai (naam aur ID ke saath), aur owner ke paas do button aate hain: "Allow ID" aur "Deny ID".
- Owner Allow dabaye to wo user panel use kar sakta hai. Deny dabaye to wo panel nahi dekh sakta. Dono ke baad user ko ek line ka message milta hai.
- Owner ke panel par "Lock bot" / "Unlock bot" button hai.

Access ke rules:
- LOCKED hai to: sirf owner aur allowed users panel use kar sakte hain.
- OPEN hai to: koi bhi user panel use kar sakta hai, jise deny nahi kiya gaya.
- Deny kiya gaya user open hone par bhi panel nahi dekh sakta, jab tak owner use dobara allow na kare.

Owner ke commands:
- /lock : bot lock karo (sirf allowed users aur owner)
- /unlock : bot open karo (jo deny nahi hue wo sab use kar sakte hain)
- /allow 123456789 : kisi user ko allow karo (ID numeric honi chahiye)
- /deny 123456789 : kisi user ko deny karo
- /users : allowed aur pending request wali IDs ki chhoti list
- Ye commands sirf owner chala sakta hai. Non-owner ko "Ye command sirf owner ke liye hai." milta hai.
- Note: Naya setup LOCKED hota hai. Purani file (jisme ye settings nahi thi) bhi LOCKED maani jaati hai, to deploy ke baad owner ek baar /unlock bhejega agar sabko use karne dena hai.

Commands: /add https://...  /remove 2  /list  /interval 5  /pingnow  /pause  /resume  /status
Owner commands: /lock  /unlock  /allow ID  /deny ID  /users

Premium look:
- Panel abhi normal emoji se chalega. pinger.py me PREMIUM_EMOJI wali list khaali hai, to koi naya premium emoji ID nahi daala gaya.
- Premium emoji ke liye Telegram ki emoji ID chahiye. Wo ID daalne par hi premium look aayega.
- Jinke Telegram par Premium nahi hai, unhe normal emoji hi dikhenge, koi error nahi aayega.
- Button ke rang (laal, hara, neela) Telegram app ke naye version par dikhte hain. Purane app me normal button dikhega.

Note:
- Free plan par file reset ho sakti hai (redeploy ke baad). Tab list PING_URLS se wapas aayegi, ya /add se phir se daal do. Access ki list (allowed aur denied) bhi isi file me hai, to reset hone par wo bhi khaali ho sakti hai.
- Token kisi ko mat dikhana. Bot ke logs me token nahi likha jata.
