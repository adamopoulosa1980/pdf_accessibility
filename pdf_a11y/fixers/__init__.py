"""Individual WCAG-checkpoint fixers."""
from .metadata import MetadataFixer
from .structure import StructureFixer
from .artifact_wrap import ArtifactWrapFixer
from .shared_xobject import SharedXObjectFixer
from .images import ImageAltTextFixer
from .contrast import ContrastFixer
from .reading_order import ReadingOrderFixer
from .tables import TableFixer
from .forms import FormFieldFixer
from .language import LanguageDetectionFixer
from .wtpdf import WTPDFFixer

__all__ = [
    "MetadataFixer",
    "StructureFixer",
    "ArtifactWrapFixer",
    "SharedXObjectFixer",
    "ImageAltTextFixer",
    "ContrastFixer",
    "ReadingOrderFixer",
    "TableFixer",
    "FormFieldFixer",
    "LanguageDetectionFixer",
    "WTPDFFixer",
]
