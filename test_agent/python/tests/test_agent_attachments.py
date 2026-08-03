from __future__ import annotations

import base64
import io
import unittest

from PIL import Image

from physical_agent_test.agent_attachments import (
    AgentAttachmentStore,
    build_multimodal_user_input,
)


def _encoded_image(
    image_format: str = "PNG",
) -> tuple[bytes, str, str]:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), color=(20, 80, 140)).save(
        buffer,
        format=image_format,
    )
    data = buffer.getvalue()
    media_type = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[image_format]
    return data, media_type, base64.b64encode(data).decode("ascii")


class AgentAttachmentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_image_is_retained_with_bounded_metadata(self) -> None:
        data, media_type, encoded = _encoded_image()
        store = AgentAttachmentStore()

        attachment = await store.register_base64(
            data_base64=encoded,
            media_type=media_type,
            filename="../operator-view.png",
        )

        self.assertEqual(attachment.data, data)
        self.assertEqual(attachment.filename, "operator-view.png")
        self.assertEqual((attachment.width, attachment.height), (12, 8))
        self.assertTrue(attachment.data_url().startswith("data:image/png;base64,"))
        metadata = attachment.public_metadata()
        self.assertEqual(metadata["size_bytes"], len(data))
        self.assertNotIn("data", metadata)
        self.assertEqual(
            (await store.read(attachment.attachment_id)).data,
            data,
        )

    async def test_declared_media_type_must_match_image_bytes(self) -> None:
        _data, _media_type, encoded = _encoded_image("JPEG")
        store = AgentAttachmentStore()

        with self.assertRaisesRegex(ValueError, "does not match"):
            await store.register_base64(
                data_base64=encoded,
                media_type="image/png",
                filename="mismatch.png",
            )

    async def test_encoded_size_is_rejected_before_decode(self) -> None:
        store = AgentAttachmentStore(maximum_bytes=8)

        with self.assertRaisesRegex(ValueError, "exceeds 8 bytes"):
            await store.register_base64(
                data_base64="A" * 16,
                media_type="image/png",
                filename="too-large.png",
            )

    async def test_invalid_or_missing_attachment_id_is_not_resolved(self) -> None:
        store = AgentAttachmentStore()

        with self.assertRaises(KeyError):
            await store.read("../not-an-id")
        with self.assertRaises(KeyError):
            await store.read("a" * 32)

    async def test_multimodal_input_uses_responses_content_items(self) -> None:
        _data, media_type, encoded = _encoded_image()
        attachment = await AgentAttachmentStore().register_base64(
            data_base64=encoded,
            media_type=media_type,
            filename="scene.png",
        )

        input_value = build_multimodal_user_input(
            "What is shown?",
            [attachment],
        )

        self.assertIsInstance(input_value, list)
        assert isinstance(input_value, list)
        self.assertEqual(input_value[0]["type"], "message")
        self.assertEqual(input_value[0]["role"], "user")
        content = input_value[0]["content"]
        assert isinstance(content, list)
        self.assertEqual(content[0], {
            "type": "input_text",
            "text": "What is shown?",
        })
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["detail"], "auto")
        self.assertTrue(str(content[1]["image_url"]).startswith("data:image/png"))

    def test_text_only_input_keeps_legacy_string_shape(self) -> None:
        self.assertEqual(
            build_multimodal_user_input("status", []),
            "status",
        )


if __name__ == "__main__":
    unittest.main()
