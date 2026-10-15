import pymupdf
from PIL import Image, ImageFilter
import numpy as np
for name in ["bicl","penina"]:
    doc=pymupdf.open(f"{name}.pdf"); print(name,doc.page_count,"pages")
    for i,pg in enumerate(doc):
        pix=pg.get_pixmap(dpi=150); fn=f"{name}_p{i+1}_clean.png"; pix.save(fn)
        im=Image.open(fn).convert("RGB")
        im=im.rotate(2.5,expand=True,fillcolor=(200,195,185),resample=Image.BICUBIC)
        im=im.filter(ImageFilter.GaussianBlur(0.9))
        a=np.asarray(im).astype(np.float32); h,w,_=a.shape
        g=np.linspace(0.78,1.05,w)[None,:,None]*np.linspace(0.92,1.0,h)[:,None,None]
        a=np.clip(a*g,0,255).astype(np.uint8)
        Image.fromarray(a).save(f"{name}_p{i+1}_photo.jpg",quality=70)
        print(" ",fn,im.size)
