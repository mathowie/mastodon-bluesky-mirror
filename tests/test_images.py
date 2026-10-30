"""Image upload, full-thread media fidelity and recovery inventory coverage."""
import io
import json
import random
from types import SimpleNamespace

import pytest
from PIL import Image
from atproto_client.models.blob_ref import BlobRef

from app import database
from app.core.transformer import ContentTransformer
from test_sync_regressions import db, status, bluesky_client, syncer, CID, DID


def encoded_image(size=(48,64), mode="RGB", color=(30,100,160), format="JPEG", **kwargs):
    out=io.BytesIO()
    Image.new(mode,size,color).save(out,format=format,**kwargs)
    return out.getvalue()


@pytest.fixture(scope="module")
def large_jpeg():
    rng=random.Random(20261029)
    image=Image.frombytes("RGB",(1400,1600),rng.randbytes(1400*1600*3))
    out=io.BytesIO();image.save(out,"JPEG",quality=100,subsampling=0)
    data=out.getvalue()
    assert len(data)>2_000_000
    return data


def uploaded_blob(data):
    mime="image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return BlobRef.model_validate({"$type":"blob","ref":{"$link":CID},"mimeType":mime,"size":len(data)})


def capture_upload(bsky,monkeypatch):
    calls=[]
    def upload(data):
        calls.append(data)
        return SimpleNamespace(blob=uploaded_blob(data))
    monkeypatch.setattr(bsky.client,"upload_blob",upload)
    return calls


def test_large_photo_is_compressed_before_upload_without_reducing_dimensions(monkeypatch,large_jpeg):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    bsky.upload_image(large_jpeg,"An accessible description")
    assert len(calls)==1
    assert len(calls[0])<=2_000_000
    with Image.open(io.BytesIO(calls[0])) as image:
        assert image.size==(1400,1600)
        assert image.format=="JPEG"


def test_small_valid_image_is_uploaded_unchanged(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    original=encoded_image()
    bsky.upload_image(original)
    assert calls==[original]


def test_oversized_dimensions_are_resized_with_aspect_ratio_preserved(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    bsky.upload_image(encoded_image(size=(5000,1000)))
    with Image.open(io.BytesIO(calls[0])) as image:
        assert image.size==(4000,800)


def test_exif_orientation_is_applied_before_metadata_is_removed(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    exif=Image.Exif();exif[274]=6
    bsky.upload_image(encoded_image(size=(80,40),exif=exif))
    with Image.open(io.BytesIO(calls[0])) as image:
        assert image.size==(40,80)
        assert image.getexif().get(274,1)==1


def test_transparent_png_keeps_transparency(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    original=encoded_image(mode="RGBA",color=(40,100,160,0),format="PNG")
    bsky.upload_image(original)
    with Image.open(io.BytesIO(calls[0])) as image:
        assert image.mode=="RGBA"
        assert image.getpixel((0,0))[3]==0


def test_corrupt_image_is_rejected_before_upload(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    with pytest.raises(ValueError,match="image"):
        bsky.upload_image(b"this is not an image")
    assert calls==[]


class Download:
    def __init__(self,data):self.data=data
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def raise_for_status(self):pass
    def iter_content(self,chunk_size):
        for start in range(0,len(self.data),chunk_size):yield self.data[start:start+chunk_size]


def media(alt="A photo beside the text"):
    return {"id":"99","type":"image","url":"https://media.example/photo.jpg","description":alt}


def test_long_post_attaches_compressed_photo_and_alt_only_to_root(db,monkeypatch,large_jpeg):
    bsky,records=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    monkeypatch.setattr("app.core.syncer.requests.get",lambda *args,**kwargs:Download(large_jpeg))
    runner=syncer(db,bsky,[status(content="<p>"+"long post "*45+"</p>",media_attachments=[media()])])
    runner.run_sync_cycle()
    assert len(records)==2
    assert records[0].embed.images[0].alt=="A photo beside the text"
    assert records[0].embed.images[0].image.size<=2_000_000
    assert records[0].embed.images[0].aspect_ratio is not None
    assert records[0].embed.images[0].aspect_ratio.width==1400
    assert records[0].embed.images[0].aspect_ratio.height==1600
    assert records[1].embed is None
    assert len(calls)==1
    assert db.get(database.SyncMap,"100000000000000001").status=="success"
    frozen=json.loads(db.get(database.SyncPart,("100000000000000001",0)).record_json)
    assert len(frozen["embed"]["images"])==1
    assert frozen["embed"]["images"][0]["alt"]=="A photo beside the text"


def test_failure_to_download_photo_never_publishes_a_text_only_thread(db,monkeypatch):
    bsky,records=bluesky_client(monkeypatch)
    def unavailable(*args,**kwargs):raise RuntimeError("Image unavailable")
    monkeypatch.setattr("app.core.syncer.requests.get",unavailable)
    syncer(db,bsky,[status(media_attachments=[media()])]).run_sync_cycle()
    assert records==[]
    assert db.get(database.SyncMap,"100000000000000001").status=="failed"


def test_declared_image_with_no_url_is_not_silently_dropped():
    with pytest.raises(ValueError,match="URL"):
        ContentTransformer.process_media(status(media_attachments=[{"type":"image","description":"A cat"}]))


def test_planning_rejects_an_accidentally_omitted_image(db,monkeypatch):
    bsky,records=bluesky_client(monkeypatch)
    runner=syncer(db,bsky,[status(media_attachments=[media()])])
    monkeypatch.setattr(runner,"_images",lambda post:[])
    runner.run_sync_cycle()
    assert records==[]
    assert db.get(database.SyncMap,"100000000000000001").status=="failed"


def test_recovery_cannot_mark_a_text_only_post_complete_if_source_has_photo(db,tmp_path):
    ident="100000000000000001"
    uri=f"at://{DID}/app.bsky.feed.post/2222222222222"
    journal={"mastodon_id":ident,"repo":DID,"source_images":[{"alt":"A photo"}],"parts":[
        {"record_key":"2222222222222","text":"First","state":"published","uri":uri,"cid":CID,"record":{"$type":"app.bsky.feed.post","text":"First","createdAt":"2026-10-29T12:00:00Z"}}
    ]}
    (tmp_path/(ident+".json")).write_text(json.dumps(journal))
    db.add(database.SyncMap(mastodon_id=ident,status="failed"));db.commit()
    assert database.import_recovery_journals(db,tmp_path)==0
    row=db.get(database.SyncMap,ident)
    assert row.status=="needs_review"
    assert "image" in row.error_message.lower()
    assert db.query(database.SyncPart).count()==1
    assert database.import_recovery_journals(db,tmp_path)==0


def test_recovery_without_source_media_inventory_requires_review(db,tmp_path):
    ident="100000000000000001"
    uri=f"at://{DID}/app.bsky.feed.post/2222222222222"
    journal={"mastodon_id":ident,"repo":DID,"parts":[
        {"record_key":"2222222222222","text":"First","state":"published","uri":uri,"cid":CID,"record":{"$type":"app.bsky.feed.post","text":"First","createdAt":"2026-10-29T12:00:00Z"}}
    ]}
    (tmp_path/(ident+".json")).write_text(json.dumps(journal))
    db.add(database.SyncMap(mastodon_id=ident,status="failed"));db.commit()
    assert database.import_recovery_journals(db,tmp_path)==0
    assert db.get(database.SyncMap,ident).status=="needs_review"


def test_oversized_transparent_png_is_reduced_without_flattening_alpha(monkeypatch):
    bsky,_=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    rng=random.Random(19)
    image=Image.frombytes("RGBA",(1000,1000),rng.randbytes(4_000_000))
    output=io.BytesIO();image.save(output,"PNG")
    assert len(output.getvalue())>2_000_000
    bsky.upload_image(output.getvalue())
    assert len(calls[0])<=2_000_000
    with Image.open(io.BytesIO(calls[0])) as prepared:
        assert prepared.format=="PNG"
        assert prepared.mode=="RGBA"
        assert prepared.getchannel("A").getextrema()[0]<255


def test_four_images_preserve_order_alt_and_first_part_placement(db,monkeypatch):
    bsky,records=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    data=encoded_image()
    monkeypatch.setattr("app.core.syncer.requests.get",lambda *args,**kwargs:Download(data))
    attachments=[dict(media(alt=f"Photo {i}"),id=str(i),url=f"https://media.example/{i}.jpg") for i in range(1,5)]
    syncer(db,bsky,[status(content="<p>"+"long post "*45+"</p>",media_attachments=attachments)]).run_sync_cycle()
    assert len(records)==2
    assert [image.alt for image in records[0].embed.images]==["Photo 1","Photo 2","Photo 3","Photo 4"]
    assert records[1].embed is None
    assert len(calls)==4


def test_partial_thread_retry_keeps_the_uploaded_photo_without_uploading_again(db,monkeypatch):
    from test_retry_features import rejection
    bsky,records=bluesky_client(monkeypatch)
    calls=capture_upload(bsky,monkeypatch)
    monkeypatch.setattr("app.core.syncer.requests.get",lambda *args,**kwargs:Download(encoded_image()))
    create=bsky.client.app.bsky.feed.post.create
    failing=[True]
    def second_fails(repo,record,**kwargs):
        if records and failing[0]:raise rejection()
        return create(repo,record,**kwargs)
    monkeypatch.setattr(bsky.client.app.bsky.feed.post,"create",second_fails)
    runner=syncer(db,bsky,[status(content="<p>"+"long post "*45+"</p>",media_attachments=[media()])])
    runner.run_sync_cycle()
    assert len(records)==1
    assert len(records[0].embed.images)==1
    db.get(database.SyncMap,"100000000000000001").status="queued";db.commit()
    failing[0]=False
    runner.run_sync_cycle()
    assert len(records)==2
    assert len(calls)==1
    assert records[1].embed is None
    assert db.get(database.SyncMap,"100000000000000001").status=="success"
