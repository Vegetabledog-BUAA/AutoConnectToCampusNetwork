import os
import base64
import hashlib
import subprocess
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

def get_encryption_key():
    """生成基于电脑固定参数的加密密钥"""
    # 使用电脑的固定参数（如用户名、系统路径等）
    unique_data = ""
    # 生成 SHA256 哈希值，并截取前 32 字节作为 AES 密钥
    return hashlib.sha256(unique_data.encode('utf-8')).digest()

def encrypt_data(data):
    """加密数据"""
    key = get_encryption_key()
    iv = os.urandom(16)  # 随机生成 16 字节的初始化向量
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()

    # 使用 PKCS7 填充数据
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded_data = padder.update(data.encode('utf-8')) + padder.finalize()

    # 加密数据
    encrypted_data = encryptor.update(padded_data) + encryptor.finalize()

    # 返回 Base64 编码的加密数据（包含 IV）
    return base64.b64encode(iv + encrypted_data).decode('utf-8')

def decrypt_data(encrypted_data):
    """解密数据"""
    key = get_encryption_key()
    encrypted_data = base64.b64decode(encrypted_data)

    # 提取 IV 和加密数据
    iv = encrypted_data[:16]
    encrypted_data = encrypted_data[16:]

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()

    # 解密数据
    padded_data = decryptor.update(encrypted_data) + decryptor.finalize()

    # 去除 PKCS7 填充
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    data = unpadder.update(padded_data) + unpadder.finalize()

    return data.decode('utf-8')
