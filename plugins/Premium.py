from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import motor.motor_asyncio
from datetime import datetime, timedelta
from config import Config

# Connect to MongoDB
db_client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URI)
db = db_client["ForwardBot"]
users_col = db["PremiumStatus"]

# === YOUR TELEGRAM ID ===
ADMIN_ID = 7689365869 

PREMIUM_TEXT = """
🚫 **PREMIUM FEATURE ONLY** 🚫

Your 1-Hour Free Trial has ended! To continue using the forwarder, please choose a plan below.

💎 **PREMIUM PLANS:**
• 1 Month - ₹50
• 3 Months - ₹120
• 5 Months - ₹150
• 1 Year - ₹300

💳 **Payment UPI:** `hodystoll@upi`
"""

# ==========================================
# ADMIN COMMANDS (With User Notification)
# ==========================================

@Client.on_message(filters.command("addprem") & filters.user(ADMIN_ID))
async def add_premium(client, message: Message):
    if len(message.command) != 3:
        return await message.reply_text("Usage: `/addprem UserID Days` \nExample: `/addprem 12345 30`")
    
    try:
        target_id = int(message.command[1])
        days = int(message.command[2])
        expiry_date = datetime.utcnow() + timedelta(days=days)
        
        await users_col.update_one(
            {"user_id": target_id}, 
            {"$set": {"is_premium": True, "expiry": expiry_date}}, 
            upsert=True
        )
        
        # Send confirmation to ADMIN
        await message.reply_text(f"✅ User `{target_id}` granted Premium for {days} days!")

        # SEND NOTIFICATION TO THE USER
        try:
            await client.send_message(
                chat_id=target_id,
                text=f"🎉 **Premium Activated!**\n\nYour account has been upgraded to Premium for **{days} days**.\n\n📅 **Expiry Date:** {expiry_date.strftime('%Y-%m-%d')}\n🚀 You can now forward messages without limits!"
            )
        except Exception as e:
            await message.reply_text(f"⚠️ Premium added, but couldn't message user: {e}")

    except ValueError:
        await message.reply_text("Invalid ID or Days. Use numbers only.")

@Client.on_message(filters.command("rmprem") & filters.user(ADMIN_ID))
async def remove_premium(client, message: Message):
    if len(message.command) != 2:
        return await message.reply_text("Usage: `/rmprem UserID`")
    target_id = int(message.command[1])
    await users_col.update_one({"user_id": target_id}, {"$set": {"is_premium": False, "expiry": None}}, upsert=True)
    await message.reply_text(f"❌ User `{target_id}` Premium revoked.")

# ==========================================
# GATEKEEPER (1-Hour Trial & Expiry Check)
# ==========================================

@Client.on_message(filters.incoming & filters.private, group=-1)
async def gatekeeper(client, message: Message):
    user_id = message.from_user.id
    text = message.text or ""
    
    if text.startswith(("/start", "/help", "/addprem", "/rmprem")):
        return 

    if user_id == ADMIN_ID:
        return 
        
    user_data = await users_col.find_one({"user_id": user_id})
    now = datetime.utcnow()

    # 1. NEW USER? Start 1-hour trial
    if not user_data:
        await users_col.insert_one({"user_id": user_id, "is_premium": False, "trial_start": now})
        return 

    # 2. CHECK PREMIUM EXPIRY
    if user_data.get("is_premium"):
        expiry = user_data.get("expiry")
        if expiry and now > expiry:
            # SUBSCRIPTION EXPIRED MESSAGE
            await users_col.update_one({"user_id": user_id}, {"$set": {"is_premium": False}})
            await message.reply_text(
                "⏰ **Your Premium has Expired!**\n\nYour subscription period is over. Please extend your premium to continue using the bot.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Extend Premium", url="https://t.me/Amirkhan_Adminbot")]
                ])
            )
            raise StopPropagation
        return 
        
    # 3. CHECK TRIAL EXPIRY (1 Hour)
    trial_start = user_data.get("trial_start")
    if trial_start and now < trial_start + timedelta(hours=1):
        return 
            
    # 4. IF NO TRIAL & NO PREMIUM: Show Main Menu
    await message.reply_text(
        text=PREMIUM_TEXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay / Contact Admin", url="https://t.me/Amirkhan_Adminbot")],
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/HodyStoll")]
        ])
    )
    raise StopPropagation



