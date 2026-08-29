"""Medición de la calidad de extracción: el conjunto dorado y sus métricas.

Por qué existe: sin una medida, cualquier cambio en el OCR es fe. "Subimos a 300 dpi"
y "activamos el deskew" suenan a mejora, pero también cuestan tiempo por página, y
alguno puede empeorar el resultado en este corpus concreto. Este paquete convierte esa
discusión en una tabla de antes/después.

La métrica que MANDA es el **recall de anclas**, no el CER. Un texto con un 8% de
caracteres mal pero con todas las CURP legibles produce las entidades correctas; un
texto con 2% de error que se comió el dígito verificador de una CURP, no. El objetivo
del sistema es que la información sea consultable y resoluble, no que el texto sea
bonito.

  norm calidad muestrear --salida dorado/    # elige y exporta los documentos a anotar
  norm calidad evaluar   --conjunto dorado/  # mide la config actual contra la verdad
"""
