import glob
from PIL import Image, ImageFilter
import numpy as np
for f in sorted(glob.glob("penina_p*_clean.png")):
    im=Image.open(f).convert("RGB")
    w,h=im.size
    # perspective (phone held at an angle): top edge narrower
    d=int(w*0.06)
    coeffs=None
    src=[(0,0),(w,0),(w,h),(0,h)]; dst=[(d,int(h*0.02)),(w-d//2,0),(w,h),(0,h-int(h*0.01))]
    A=[];B=[]
    for (x,y),(X,Y) in zip(dst,src):
        A.append([x,y,1,0,0,0,-X*x,-X*y]);A.append([0,0,0,x,y,1,-Y*x,-Y*y]);B+= [X,Y]
    c=np.linalg.solve(np.array(A,float),np.array(B,float))
    im=im.transform((w,h),Image.PERSPECTIVE,tuple(c),Image.BICUBIC,fillcolor=(120,110,100))
    im=im.rotate(-5,expand=True,fillcolor=(120,110,100),resample=Image.BICUBIC)
    im=im.resize((int(im.width*0.62),int(im.height*0.62)),Image.BILINEAR)  # ~95 dpi effective
    im=im.filter(ImageFilter.GaussianBlur(1.3))
    a=np.asarray(im).astype(np.float32); H,W,_=a.shape
    yy,xx=np.mgrid[0:H,0:W]
    shadow=1-0.45*np.clip((xx/W-0.55)*2.5,0,1)*np.clip((yy/H-0.3)*2,0,1)  # hand/phone shadow bottom-right
    a=a*shadow[...,None]*np.linspace(0.85,1.0,W)[None,:,None]
    a+=np.random.default_rng(0).normal(0,6,a.shape)
    Image.fromarray(np.clip(a,0,255).astype(np.uint8)).save(f.replace("_clean.png","_harsh.jpg"),quality=50)
