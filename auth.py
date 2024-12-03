from dotenv import load_dotenv
import os
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Union, Any
from fastapi import Depends, HTTPException, status, APIRouter, Response, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from schemes import User
from models import OsMember, APIKeyLog
from conn_postgre import get_db
import database
from conn_arduino.dec_data import conn_hsm
from log_manage import login
load_dotenv()
# router 설정
verify_router = APIRouter(prefix="/api")

# Redis Connect and receive {key : value}
rd = database.redis_config()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# login 시 호출 될 API
# 카드에 담겨 온 Response와 Challenge 값 검증 및 UUID 검증
@verify_router.post("/v1/card-response")
async def verify_card_response(data, gen_challenge, response : Response, db: Session = Depends(get_db)):
    try:
        import binascii
        # 앱에서 전달된 데이터 복호화
        dec_data = conn_hsm.decrypt(data=binascii.unhexlify(data))
        
        # Redis에서 기존 challenge 키로 저장된 값 조회
        stored_challenge = rd.get(gen_challenge)
        if stored_challenge is None:
            raise HTTPException(status_code=404, detail="Key not found or expired")
        
        print(f"Challenge in Redis: {stored_challenge.decode()}")  # Redis에서 가져온 챌린지 값 출력
        print(f"Decrypted data: {dec_data.hex()}")  # 복호화된 데이터 출력
        
        # 챌린지 값만 추출 (예시, 실제 데이터 구조에 맞게 수정 필요)
        dec_challenge = dec_data.hex()  
        print(f"Extracted challenge: {dec_challenge}")
        
        # 저장된 챌린지 값과 비교
        if stored_challenge.decode() == dec_challenge:
            print("Challenge match: correct")
        else:
            print("Challenge match: incorrect")
            raise HTTPException(status_code=400, detail="Invalid response")
        
        # UUID 확인
        member_uuid = db.query(OsMember).filter(OsMember.uuid == dec_data.hex()).first()
        if not member_uuid:
            print("Member not found in database")
            raise HTTPException(status_code=404, detail="Member not found")
        
        import uuid
        s_id = str(uuid.uuid4()) # 세션 아이디 하나 생성
        response.set_cookie(key="s_id", value=s_id, httponly=True)
        return {"message": "NFC Authentication Successful", "s_id" : {s_id}}
    
    except Exception as e:
        print(f"Error occurred: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

### 인가 코드 발급 ### 
# 서비스 서버에서 인가 코드 요청(로그인(유저 검증)을 한 후) 했을 시 기능 수행
@verify_router.get("/v1/authorization-code")
def issue_authorization_code(API_KEY : str, redirect_uri : str, response : Response, request : Request, db : Session = Depends(get_db)):
    user_api_key = db.query(APIKeyLog).filter(APIKeyLog.key == API_KEY).first() # 생성된 API KEY SELECT 
    session_id = login()
    s_id = request.cookies.get("s_id") # session id 
    try:
        if not user_api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Invalid API KEY")
        if not session_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Unauthorized User")
        
        
        # 인가 코드 생성
        authorization_code = hex(random.getrandbits(128))[2:]
        
        # Redis INSERT -> key : value (session id : authorization code)  
        rd.set(s_id,authorization_code)
        
        # 리디렉션 URL에 인가 코드를 포함하여 반환
        redirect_url = f"{redirect_uri}?code={authorization_code}" # redirect_url 
        response.status_code = status.HTTP_302_FOUND
        response.headers["Location"] = redirect_url
        return {"message" : "Redirecting with authorization code"}       
    except:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Data for authorization_code")

@verify_router.get("/v1/callback")
async def ospass_login_callback(code : str, request : Request):
    try:
        s_id = request.cookies.get("s_id") # cookie에서 s_id 가져옴
        if not s_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,detail="Session ID not found")
        auth_code = rd.get(s_id)
        if not auth_code:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,detail="Authorization Code not found")
        if auth_code.decode() != code:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,detail="Invalid Authorization code")
        access_token = await issue_access_token(code)
        return access_token
    except:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid Authorization code")

@verify_router.post("/v1/access-token")
def issue_access_token(code: str, request : Request,expires_delta: timedelta | None = None):
    # 1. Redis에서 s_id 및 Authorization Code 조회
    s_id = request.cookies.get("s_id")  # 클라이언트 쿠키에서 s_id 가져옴
    if not s_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Session ID not found")
    
    stored_code = rd.get(s_id)  # Redis에서 s_id를 키로 저장된 authorization_code 조회
    if not stored_code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Authorization Code not found")

    # 2. Authorization Code 검증
    stored_code = stored_code.decode()  # Redis 값은 bytes 타입이므로 decode 필요
    if stored_code != code:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Authorization Code")

    # 3. 액세스 토큰 생성
    to_encode = {
        "sub": s_id,  # 사용자 세션 ID
        "iat": datetime.now(ZoneInfo("Asia/Seoul")).timestamp()  # 토큰 발급 시간
    }
    expire = datetime.now(ZoneInfo("Asia/Seoul")) + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})  # 토큰 만료 시간 설정

    access_token = jwt.encode(to_encode, os.getenv("ACCESS_SECRET_KEY"), algorithm=os.getenv("ALGORITHM"))

    # 4. Redis에서 Authorization Code 삭제 (재사용 방지)
    rd.delete(s_id)

    # 5. 반환
    return {"access_token": access_token, "token_type": "bearer"}
 







def authentication_user(db, session_id : str):
    user = get_user(db, session_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,detail="Invalid Session_ID")

#-------------------------------------------------------#
# 사용자 정보(UUID, 이름, 포지션(0=부원, 1=랩장)) 가져오기
@verify_router.get("/v1/userinfo")
def get_user( db: Session = Depends(get_db)):
    try:
        members = db.query(OsMember).all()
        if not members:
            raise HTTPException(status_code=404, detail="Data Not Found")
        
        return [User(uuid=member.uuid, name=member.name, position=member.position) for member in members]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
